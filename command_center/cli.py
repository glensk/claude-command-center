#!/usr/bin/env python3
"""``ccc`` command-line entry point.

Subcommands:
  ls                      flat clickable list of every tracked session
  aim                     fast store lookup for the status line (no LLM)
  set-aim / set-next / set-blocked / set-deadline / set-donecheck
  subgoals / check        manage the progress checklist
  mark-done / keep        lifecycle flags
  resume <id>             resume a session in this terminal
  peek                    show my last prompt for the iTerm-focused session (panel)
  jump                    focus the iTerm tab running the ccc TUI (Karabiner chord)
  hook <event>            invoked by Claude Code hooks (phase 2)
  daemon                  idle-reaper + summary regen (phase 3)
  tui / serve             Textual UI, terminal and browser (phases 4-5)
"""

# Lazy imports (import-outside-toplevel) keep `ccc ls`/`aim`/`statusline` fast by
# not importing textual/daemon/llm at startup. Command handlers share a uniform
# (args) signature, so some ignore it (unused-argument). build_parser is long by
# nature (too-many-statements).
# pylint: disable=import-outside-toplevel,unused-argument,too-many-statements

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import argparse
import json
import os
import re
import sys

from . import __version__, config, llmrouting
from .adapters import ClaudeAdapter
from .models import (
    DEFAULT_LLM,
    EFFORT_LEVELS,
    JOB_TYPES,
    LLM_AGENT_ALIAS,
    LLM_CHOICES,
    LLM_MODEL_IDS,
    Session,
    Status,
    Subgoal,
    aim_column_first,
    days_until_start,
    empty_track_tint,
    no_codex_conflict,
    now_ms,
    parse_iso_date,
    xterm_rgb,
)
from .store import Store

# Seconds `ccc close-now` waits before reaping/closing, so the render + the rest of the
# Stop-hook chain (auto-commit) settle first. Module-level so tests can monkeypatch it.
_CLOSE_NOW_SETTLE_SEC = 2.0


def _adapter() -> ClaudeAdapter:
    return ClaudeAdapter()


def _close_arming_suppressed() -> bool:
    """True when ``mark-done --close`` must NOT arm a close (headless / SDK entrypoint).

    Mirrors ``hooks._is_headless``: a ``claude -p`` run (``CCC_INTERNAL`` set, or
    ``CLAUDE_CODE_ENTRYPOINT`` starting with ``sdk``) is not a real interactive tab to
    close, so arming is a no-op there (a stderr note; mark-done still succeeds).
    """
    return bool(os.environ.get("CCC_INTERNAL")) or os.environ.get(
        "CLAUDE_CODE_ENTRYPOINT", ""
    ).startswith("sdk")


def resolve_session_id(adapter: ClaudeAdapter, explicit: str | None, cwd: str | None) -> str | None:
    """Resolve a session id: explicit > $CLAUDE_SESSION_ID > the live session in *cwd*."""
    if explicit:
        return explicit
    env = os.environ.get("CLAUDE_SESSION_ID")
    if env:
        return env
    cwd = cwd or os.getcwd()
    candidates = [s for s in adapter.discover() if s.cwd == cwd]
    if not candidates:
        return None
    candidates.sort(key=lambda s: (s.raw_status != "busy", -s.updated_at))
    return candidates[0].session_id


def _spawn_sync_mirrors(cfg: config.Config) -> None:
    """Fire a detached ``ccc sync-mirrors`` after a lifecycle mutation (best-effort).

    Gated on the mirror kill-switches, and skipped inside a headless ``claude -p``
    (``CCC_INTERNAL``) — like ``futuresync._spawn_aim_jobs`` — so a detached ccc never
    re-spawns and unit tests never fork a real ccc. The daemon still re-syncs each pass.
    """
    if not (cfg.mirror_running or cfg.mirror_done or cfg.mirror_sessions) or os.environ.get(
        "CCC_INTERNAL"
    ):
        return
    from .spawn import spawn_ccc

    spawn_ccc(["sync-mirrors"])


# --------------------------------------------------------------------------- #
# command handlers
# --------------------------------------------------------------------------- #
def cmd_llm_routing(_args: argparse.Namespace) -> int:
    """`ccc llm-routing` — the same table `ccc --help` embeds, on demand."""
    print(llmrouting.render(), end="")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    from .views import ls as ls_view

    cfg = config.load_config()
    with Store() as store:
        print(
            ls_view.render(
                store,
                _adapter(),
                warn_days=cfg.deadline_warn_days,
                folder_order=tuple(cfg.folder_order),
                aim_threshold=cfg.aim_score_threshold,
                aim_first=aim_column_first(cfg.aim_column),
            )
        )
    return 0


def _paint_bar_glyphs(seg: str, fill: str, empty: str, reset: str) -> str:
    """Colour a run of progress-bar glyphs: ``▓``/``█`` in *fill*, ``░`` in *empty* (ANSI)."""
    return "".join(f"{fill if ch in '▓█' else empty}{ch}{reset}" for ch in seg)


def _paint_done_word(
    done_word: str, fills: tuple[bool, ...], fill_code: int, empty_code: int
) -> str:
    """Red DONE letters over the bar. The DONE bar's filled cells are solid ``█`` in
    *fill_code*, and a filled-cell letter's background is that SAME palette entry — so letter
    cells and bar cells render pixel-identically, no seam. An empty-cell letter gets a faint
    25 % tint of *empty_code* (``empty_track_tint``), the ``░`` track's average (ANSI)."""
    parts = []
    for ch, filled in zip(done_word, fills, strict=True):
        if filled:
            parts.append(f"\033[1;38;5;196;48;5;{fill_code}m{ch}\033[0m")
        else:
            r, g, b = empty_track_tint(xterm_rgb(empty_code))
            parts.append(f"\033[1;38;5;196;48;2;{r};{g};{b}m{ch}\033[0m")
    return "".join(parts)


def cmd_aim(args: argparse.Namespace) -> int:
    """Fast, LLM-free lookup for the status line. Must stay well under ~10 ms."""
    from .models import done_bar_parts, effective_progress, progress_bar

    adapter = _adapter()
    session_id = resolve_session_id(adapter, args.session, None)
    if not session_id:
        return 0
    with Store() as store:
        session = store.get(session_id)
        if session is None or not session.aim:
            if args.format == "statusline":
                print("🎯 set done-condition  (/aim)")
            elif args.format == "bar":
                print("\033[38;5;244m🎯 /aim\033[0m")  # main-line placeholder, no AIM yet
            return 0
        if args.format == "plain":
            print(session.aim)
            return 0
        checked, total = store.progress(session_id)
        fraction = effective_progress(session.manual_progress, checked, total)
        if args.format == "bar":
            # Compact, colored progress bar for the main status line (line 1). Filled
            # cells green, empty dim; ``progress_bar`` stays the single glyph source.
            green, dim, reset = "\033[38;5;42m", "\033[38;5;244m", "\033[0m"
            pct = f" {int(round(fraction * 100))}%" if fraction is not None else ""
            if session.aim_met:
                # Impartial checker judged the AIM fulfilled → red DONE stamped inside the
                # bar; letters over filled cells carry the green fill as background.
                left, done_word, right, fills = done_bar_parts(fraction, 8)
                bar = _paint_bar_glyphs(left, green, dim, reset)
                bar += _paint_done_word(done_word, fills, 42, 244)
                bar += _paint_bar_glyphs(right, green, dim, reset)
                print(f"{bar}{pct}")
            elif fraction is None:
                print(f"{dim}{'░' * 8}{reset}")  # AIM set, no checklist yet
            else:
                plain = progress_bar(fraction, 8)
                filled = plain.count("▓")
                bar = f"{green}{'▓' * filled}{reset}{dim}{'░' * (8 - filled)}{reset}"
                print(f"{bar}{pct}")
            return 0
        parts = [f"🎯 {session.aim}"]
        if session.aim_met:  # red DONE stamped inside the bar (same overlay as every bar site)
            left, done_word, right, fills = done_bar_parts(fraction, 8)
            pct = f" {int(round(fraction * 100))}%" if fraction is not None else ""
            # The DONE bar's glyph runs are painted 231 (white) — the SAME palette entry the
            # filled letters use as background — so █ cells and letter cells match exactly.
            white = "\033[38;5;231m"
            parts.append(
                f"{white}{left}\033[0m"
                f"{_paint_done_word(done_word, fills, 231, 231)}"
                f"{white}{right}\033[0m{pct}"
            )
        elif fraction is not None:
            parts.append(f"{progress_bar(fraction, 8)} {int(round(fraction * 100))}%")
        if session.deadline:
            parts.append(f"⏰{session.deadline}")
        if session.blocked_on:
            parts.append(f"⛔{session.blocked_on}")
        print("  ".join(parts))
    return 0


def cmd_todos(args: argparse.Namespace) -> int:
    """Print the session's live todo list (forwarded from TodoWrite / the Task tools)."""
    from .models import loads_todos, todos_counts

    adapter = _adapter()
    session_id = resolve_session_id(adapter, args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        session = store.get(session_id)
    todos = loads_todos(session.todos) if session else []
    if not todos:  # store has no snapshot yet — read the live on-disk list directly
        todos = adapter.todos(session_id, session.config_dir if session else None)
    if not todos:
        print("(no todos for this session yet)")
        return 0
    done, total = todos_counts(todos)
    print(f"todos {done}/{total} done — {session_id[:8]}")
    for status, subject in todos:
        box = "[x]" if status == "completed" else ("[~]" if "progress" in status else "[ ]")
        print(f"  {box} {subject}")
    return 0


def _set_field(args: argparse.Namespace, field: str, value: object) -> int:
    adapter = _adapter()
    session_id = resolve_session_id(adapter, args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        store.ensure(session_id, cwd=os.getcwd())
        store.update_fields(session_id, **{field: value})
    print(f"{field} set for {session_id}")
    return 0


def cmd_set_aim(args: argparse.Namespace) -> int:
    adapter = _adapter()
    session_id = resolve_session_id(adapter, args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    first = getattr(args, "first", False)
    if first and not args.text.strip():
        print(
            "error: --first cannot empty the first AIM (pass the corrected text)", file=sys.stderr
        )
        return 1
    with Store() as store:
        store.ensure(session_id, cwd=os.getcwd())
        if first:
            # Rewrite `/aim (1)` in place — no new revision, the current AIM stays as is.
            # Re-scoring only makes sense when that revision IS the current AIM (a single
            # revision); a historical row carries its own lexical score. The short label is
            # regenerated either way: it is built from the original AIM as a hint, so it went
            # stale with this rewrite (set_first_aim dropped it).
            rescore = store.count_aim_history(session_id) <= 1
            changed = store.set_first_aim(session_id, args.text)
            if not changed:
                print(f"first aim unchanged for {session_id}")
                return 0
            print(f"first aim rewritten for {session_id}")
        else:
            rescore = True
            changed = store.set_aim(session_id, args.text)  # clears stale auto checklist + offset
            print(f"aim set for {session_id}")
    # On a real change, detached so we never block the caller: (a) refine the instant
    # lexical AIM score with a cheap LLM call, and (b) regenerate the short-AIM label
    # (cheap codex run — keeps the column scannable without spending Claude tokens).
    if changed:
        cfg = config.load_config()
        from .spawn import spawn_ccc

        if cfg.aim_score_on_set and rescore:
            spawn_ccc(["score-aim", "--session", session_id])
        if cfg.short_aim:
            spawn_ccc(["short-aim", "--session", session_id])
    return 0


def cmd_score_aim(args: argparse.Namespace) -> int:
    """Score an AIM via the independent rubric checker.

    ``--dry-run "<candidate>"`` scores a *candidate* string and prints the JSON breakdown
    WITHOUT touching the store — this is the loop the in-session sharpener iterates against.
    Otherwise it refines the *stored* AIM's score (the detached entry spawned after set-aim).
    """
    import json

    from . import aimscore, llm

    candidate = getattr(args, "dry_run", None)
    if candidate is not None:
        cfg = config.load_config()
        # --dry-run scores a candidate string with no session behind it → no note.
        detail = aimscore.score_aim_detailed(candidate, cfg)
        if detail is None:  # every ladder rung failed — fall back to the instant lexical estimate
            detail = {
                "score": aimscore.score_aim_lexical(candidate),
                "criteria": {},
                "reason": "lexical estimate (LLM unavailable)",
                "missing": "",
                "backend": "lexical",
            }
        print(json.dumps(detail))
        return 0

    session_id = resolve_session_id(_adapter(), args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        session = store.get(session_id)
        if session is None or not session.aim:
            return 0
        result = aimscore.score_aim_llm(
            session.aim,
            config.load_config(),
            # note = the session's first AIM (router-log metadata: CCC_LLM_NOTE).
            note=llm.concise_note(
                next((r.aim for r in store.list_aim_history(session_id)), "") or session.aim
            ),
        )
        if result is None:
            return 0
        score, reason = result
        store.update_fields(session_id, aim_score=score, aim_score_reason=reason or None)
    print(f"aim score {score} for {session_id}")
    return 0


def cmd_short_aim(args: argparse.Namespace) -> int:
    """Generate the cheap-model short-AIM label (the scannable ``/aim`` column text).

    ``--dry-run "<aim>"`` prints the label a candidate AIM would produce WITHOUT touching
    the store (parity with ``score-aim --dry-run``). Otherwise it regenerates the *stored*
    AIM's label — this is the detached entry spawned after ``set-aim`` and the daemon
    backfill — and writes it onto the session + its latest AIM-history revision.
    """
    from . import short_aim

    cfg = config.load_config()
    candidate = getattr(args, "dry_run", None)
    if candidate is not None:
        label = short_aim.generate(
            candidate, backend=cfg.short_aim_backend, model=cfg.short_aim_model
        )
        print(label or "")
        return 0

    session_id = resolve_session_id(_adapter(), args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        session = store.get(session_id)
        if session is None or not session.aim:
            return 0
        history = store.list_aim_history(session_id)
        original = history[0].aim if history else None
        label = short_aim.generate(
            session.aim,
            original=original,
            backend=cfg.short_aim_backend,
            model=cfg.short_aim_model,
        )
        if label:
            store.set_short_aim(session_id, label)
        # Revision (1) keeps a label of its own: it is what the `/aim` column renders under
        # `aim_column = "first"`, and it does NOT come along with the current AIM's label
        # once the two diverge (set_short_aim writes the LATEST revision; `set-aim --first`
        # drops the original's stale one). Only generated when actually missing, so this
        # costs a second cheap call at most once per original.
        if original and original != session.aim and not history[0].short_aim:
            if first_label := short_aim.generate(
                original, backend=cfg.short_aim_backend, model=cfg.short_aim_model
            ):
                store.set_first_short_aim(session_id, first_label)
        if not label:
            return 0
    print(f"short-aim set for {session_id}: {label}")
    return 0


def cmd_copilot_usage(args: argparse.Namespace) -> int:
    """Refresh (via ``gh``) and print this month's GitHub Copilot usage; cache it for the card.

    Fetches the per-user enhanced-billing usage for the current month, writes
    ``copilot_usage.json`` (read cheaply by the TUI card and ``ccc ls``), and prints a
    one-line summary. ``--json`` dumps the cached snapshot instead. Spawned detached by the
    TUI/daemon when the cache is stale; also runnable by hand.
    """
    from . import usage
    from .links import osc8_link

    if getattr(args, "json", False):
        snap = usage.read_copilot_usage()
        print(json.dumps(snap.__dict__ if snap else {}, indent=2))
        return 0

    snap = usage.fetch_copilot_usage()
    if snap is None:
        print(
            "error: could not read Copilot usage (is `gh` installed and authenticated?)",
            file=sys.stderr,
        )
        return 1
    cost = f"${snap.gross:.2f} covered" if snap.net <= 1e-6 else f"${snap.net:.2f} billed"
    link = osc8_link("https://github.com/settings/billing", "github.com/settings/billing")
    # Headline the window the card draws: AI credits against the seat's entitlement on a
    # credit seat, premium requests otherwise. `?` marks a quota that is still the
    # configured guess because the per-seat endpoint did not answer.
    used = snap.credits_used or snap.quantity
    if snap.unit == "AI credits" and used > 0:
        quota = max(1, snap.credit_quota)
        mark = "" if snap.quota_source == "api" else "?"
        head = f"AI credits {used:.1f}/{quota}{mark} ({used / quota * 100:.0f}%)"
    else:
        quota = max(1, snap.premium_quota)
        pct = snap.premium_used / quota * 100
        head = f"premium requests {int(round(snap.premium_used))}/{quota} ({pct:.0f}%)"
    print(f"GitHub Copilot — this month: {head} · {cost}  ({link})")
    return 0


def cmd_claude_usage(args: argparse.Namespace) -> int:
    """Fetch each account's Claude ``/usage`` from the OAuth endpoint and cache it; warm the card.

    Mirrors ``copilot-usage`` as a best-effort out-of-band warmer: for each configured
    account (or the one named by ``-a/--account``) it fetches the OAuth usage endpoint and
    authoritatively replaces that account's usage snapshot (self-healing a rebased window
    boundary and adding the Fable weekly window the status line never carries). Prints one
    short line per account. Always exits 0 — a missing/expired token simply skips that
    account. Spawned detached by the TUI/daemon when the snapshot is stale; runnable by hand.

    Rides along with a second, far rarer fetch: an account whose card is configured
    ``subscription_ends = ["claude_…=auto"]`` also refreshes its OAuth *profile* (once a
    day, :func:`usage.claude_profile_stale`) for the billing anniversary that card's
    ``-> D.M`` suffix is derived from. No ``auto`` entry ⇒ that endpoint is never called.
    """
    from . import usage

    labels = list(config.claude_config_dirs())
    account = getattr(args, "account", None)
    if account:
        labels = [account] if account in labels else []
        if not labels:
            print(f"claude-usage: {account} not a configured account", file=sys.stderr)
            return 0
    ends = config.parse_subscription_ends(config.load_config().subscription_ends)
    derived = {
        label
        for card, label in config.SUBSCRIPTION_CARD_ACCOUNTS.items()
        if ends.get(card) == "auto"
    }
    for label in labels:
        snap = usage.fetch_claude_usage(label)
        print(
            f"claude-usage: {label} {'fetched' if snap else 'skipped (no token or fetch failed)'}"
        )
        if label in derived and usage.claude_profile_stale(label):
            created = usage.fetch_claude_profile(label)
            print(f"claude-usage: {label} subscription start {created or '(unavailable)'}")
    return 0


def cmd_codex_usage(args: argparse.Namespace) -> int:
    """Fetch each configured ``CODEX_HOME``'s live Codex usage and cache it; warm the card.

    Mirrors ``claude-usage`` as a best-effort out-of-band warmer: for each configured
    Codex login (or the one named by ``-a/--account``) it fetches
    ``chatgpt.com/backend-api/wham/usage`` — the numbers the ChatGPT *Settings → Usage*
    page shows — and caches them per home, so the card no longer waits for the next Codex
    turn to write a rollout event. ``-j/--json`` dumps the cached snapshots instead (a
    dict keyed by label, no fetch). Prints one line per login and always exits 0: a home
    with no ChatGPT ``auth.json`` simply reports the failure on stderr and is skipped.
    Spawned detached by the TUI/daemon when a cache is stale; runnable by hand.
    """
    import dataclasses
    import time

    from . import usage

    homes = config.codex_homes()
    account = getattr(args, "account", None)
    if account:
        if account not in homes:
            print(f"codex-usage: {account} not a configured CODEX_HOME", file=sys.stderr)
            return 0
        homes = {account: homes[account]}

    if getattr(args, "json", False):
        cached = {
            label: (dataclasses.asdict(snap) if (snap := usage.read_codex_live(home)) else {})
            for label, home in homes.items()
        }
        print(json.dumps(cached, indent=2))
        return 0

    now = int(time.time())
    for label, home in homes.items():
        snap = usage.fetch_codex_usage(home, now)
        if snap is None:
            print(
                f"OpenAI Codex {label}: fetch failed "
                "(no ChatGPT auth.json token, or the endpoint refused)",
                file=sys.stderr,
            )
            continue
        head = f"OpenAI Codex {snap.email or label}"
        if snap.plan_type:
            head += f" ({snap.plan_type})"
        parts = [
            f"{name} {win.used_percentage:.0f}% resets {usage.format_reset(win.resets_at, now)}"
            for name, win in (("Session", snap.five_hour), ("Week", snap.seven_day))
            if win is not None
        ]
        if snap.blocked:
            parts.append(f"BLOCKED — {snap.blocked_reason}")
        print(f"{head}: " + (" · ".join(parts) if parts else "no window data"))
    return 0


def cmd_aim_history(args: argparse.Namespace) -> int:
    """Print the session's AIM progression — every (re)definition, first to current."""
    from datetime import datetime

    from .models import short_id, synthesize_aim_revision

    session_id = resolve_session_id(_adapter(), args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        session = store.get(session_id)
        revisions = store.list_aim_history(session_id)
    # Pre-history session (AIM set before tracking began): show the live AIM as the sole entry.
    if not revisions and session and session.aim:
        revisions = [synthesize_aim_revision(session)]
    if not revisions:
        print("(no AIM set for this session yet)")
        return 0
    plural = "s" if len(revisions) != 1 else ""
    print(f"AIM history — {short_id(session_id)}  ({len(revisions)} revision{plural})")
    for index, rev in enumerate(revisions, 1):
        when = (
            datetime.fromtimestamp(rev.created_at / 1000).strftime("%Y-%m-%d %H:%M")
            if rev.created_at
            else "—"
        )
        score = f"{rev.score}%" if rev.score >= 0 else "—"
        marker = "  ← current" if index == len(revisions) else ""
        print(f"  {index}. {when}  ·  {score:>4}  {rev.aim}{marker}")
        if rev.short_aim:  # the cheap-model short label tracked for this revision
            print(f"        ↳ short: {rev.short_aim}")
    return 0


def cmd_subgoal_history(args: argparse.Namespace) -> int:
    """Print the session's sub-goal evolution — each version, with the impartial drift verdict."""
    from datetime import datetime

    from .models import short_id

    session_id = resolve_session_id(_adapter(), args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        revisions = store.list_subgoal_history(session_id)
    if not revisions:
        print("(no sub-goal history for this session yet)")
        return 0
    plural = "s" if len(revisions) != 1 else ""
    print(f"Sub-goal history — {short_id(session_id)}  ({len(revisions)} version{plural})")
    for index, rev in enumerate(revisions, 1):
        when = (
            datetime.fromtimestamp(rev.created_at / 1000).strftime("%Y-%m-%d %H:%M")
            if rev.created_at
            else "—"
        )
        checked = sum(1 for _, done in rev.items if done)
        if rev.drift_severity in ("low", "medium", "high"):
            why = f" — {rev.drift_reason}" if rev.drift_reason else ""
            drift = f"  ⚠ drift:{rev.drift_severity}{why}"
        elif rev.drift_severity == "none":
            drift = "  ✓ no drift"
        else:
            drift = "  · drift:pending"
        marker = "  ← current" if index == len(revisions) else ""
        print(
            f"  {index}. {when}  ·  {rev.trigger}  ·  from AIM v{rev.aim_rev}  ·  "
            f"{checked}/{len(rev.items)}{drift}{marker}"
        )
        for text, done in rev.items:
            print(f"       [{'x' if done else ' '}] {text}")
    return 0


def _git_toplevel(path: str) -> str | None:
    """The git repo root containing *path* (None if not in a repo / git unavailable)."""
    import subprocess

    start = path if os.path.isdir(path) else os.path.dirname(path)
    try:
        proc = subprocess.run(
            ["git", "-C", start, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


def cmd_handoff(args: argparse.Namespace) -> int:
    """Commit + push a locked file, then release it so a waiting session can take over.

    The handoff invariant: the file is committed (and pushed) BEFORE the lock drops, so the
    next session never starts on uncommitted work. If the commit/push fails, the lock is kept.
    """
    from . import gitcommit

    session_id = resolve_session_id(_adapter(), args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    path = os.path.abspath(os.path.expanduser(args.file))
    repo = _git_toplevel(path) or os.path.dirname(path)
    message = args.message or f"chore: hand off {os.path.basename(path)}"
    ok, detail = gitcommit.commit_and_push(repo, [path], message)
    if not ok:
        print(f"error: handoff commit/push failed — lock kept: {detail}", file=sys.stderr)
        return 1
    with Store() as store:
        store.release_file_lock(session_id, path)
    print(f"handed off {path} — committed + pushed, lock released")
    return 0


def cmd_lock_release(args: argparse.Namespace) -> int:
    """Force-release a file lock without committing (escape hatch; prefer `ccc handoff`)."""
    from .models import short_id

    session_id = resolve_session_id(_adapter(), args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        if args.all or not args.file:
            count = store.release_all_file_locks(session_id)
            print(f"released {count} lock(s) held by {short_id(session_id)}")
        else:
            path = os.path.abspath(os.path.expanduser(args.file))
            released = store.release_file_lock(session_id, path)
            print(f"{'released' if released else 'no lock held on'} {path}")
    return 0


def cmd_locks(args: argparse.Namespace) -> int:
    """List every active (live holder + non-stale) cross-session file lock."""
    from datetime import datetime

    from .models import short_id

    cfg = config.load_config()
    live_ids = {ls.session_id for ls in _adapter().discover() if ls.alive}
    now = now_ms()
    with Store() as store:
        locks = store.list_file_locks(live_ids, cfg.file_lock_ttl_sec * 1000, now)
    if not locks:
        print("no active file locks")
        return 0
    print(f"{len(locks)} active file lock(s):")
    for lock in locks:
        held = (
            datetime.fromtimestamp(lock.acquired_at / 1000).strftime("%H:%M")
            if lock.acquired_at
            else "—"
        )
        print(f"  {short_id(lock.session_id)}  since {held}  {lock.file_path}")
    return 0


def cmd_set_next(args: argparse.Namespace) -> int:
    adapter = _adapter()
    session_id = resolve_session_id(adapter, args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        store.ensure(session_id, cwd=os.getcwd())
        store.update_fields(session_id, next_step=args.text, next_step_source="user")
    print(f"next step set for {session_id}")
    return 0


def cmd_set_blocked(args: argparse.Namespace) -> int:
    return _set_field(args, "blocked_on", args.text or None)


def cmd_set_deadline(args: argparse.Namespace) -> int:
    from datetime import date

    if args.date:
        try:
            date.fromisoformat(args.date)
        except ValueError:
            print(
                f"error: deadline must be ISO-8601 (YYYY-MM-DD), got {args.date!r}", file=sys.stderr
            )
            return 1
    return _set_field(args, "deadline", args.date or None)


def cmd_set_donecheck(args: argparse.Namespace) -> int:
    return _set_field(args, "done_check_cmd", args.command or None)


def cmd_subgoals(args: argparse.Namespace) -> int:
    adapter = _adapter()
    session_id = resolve_session_id(adapter, args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        store.ensure(session_id, cwd=os.getcwd())
        if args.list:
            # Lead with the 1-based position — that is what `ccc check <n>` expects
            # (a per-session handle, not the global DB id, so cross-session ticks
            # are impossible).
            for position, sub in enumerate(store.list_subgoals(session_id), start=1):
                box = "[x]" if sub.checked else "[ ]"
                chk = f"  ⚙ {sub.check_cmd}" if sub.check_cmd else ""
                print(f"{position:3d} {box} {sub.text}{chk}")
            return 0
        items = [s.strip() for s in args.items if s.strip()]
        changed = store.set_subgoals(
            session_id,
            items,
            source=getattr(args, "source", "user"),
            adaptive=(True if getattr(args, "adaptive", False) else None),
            merge=getattr(args, "merge", False),
        )
        tag = " (adaptive)" if getattr(args, "adaptive", False) else ""
        print(f"{len(items)} sub-goals set for {session_id}{tag}")
    # Non-blocking nudge: flag items that aren't objectively checkable.
    from .autoprogress import lint_subgoal

    for item in items:
        reason = lint_subgoal(item)
        if reason:
            print(f"  ⚠️  vague sub-goal {item!r}: {reason}", file=sys.stderr)
    # On a real change, spawn the impartial drift checker (detached, never blocking),
    # unless we are already inside a `claude -p` (CCC_INTERNAL) — that would recurse.
    if changed and config.load_config().drift_check and not os.environ.get("CCC_INTERNAL"):
        from .spawn import spawn_ccc

        spawn_ccc(["check-drift", "--session", session_id])
    return 0


def cmd_check_drift(args: argparse.Namespace) -> int:
    """Impartial drift check of the latest sub-goal change (internal; spawned detached).

    Compares the previous vs newest sub-goal version against the original + current AIM
    via a separate cheap ``claude -p`` (never the session agent), then records the verdict
    on the session (the blue dot) and the history row. First-ever version can't drift.
    """
    import json

    from . import drift, llm

    cfg = config.load_config()
    if not cfg.drift_check:
        return 0
    session_id = resolve_session_id(_adapter(), args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        session = store.get(session_id)
        if session is None:
            return 0
        history = store.list_subgoal_history(session_id)
        if len(history) < 2:  # nothing to compare against -> not drift
            store.set_drift(session_id, "none", None)
            return 0
        previous, current = history[-2], history[-1]
        evolution = [r.aim for r in store.list_aim_history(session_id)] or [session.aim or ""]
        facts = drift.build_facts(
            evolution[0],
            session.aim,
            evolution,
            previous.items,
            [text for text, _ in current.items],
        )
        verdict = drift.check_drift(
            facts,
            cfg.drift_model or cfg.llm_model,
            note=llm.concise_note(evolution[0] or session.aim),
        )
        if verdict is None:
            return 0
        store.set_drift(session_id, verdict["severity"], verdict["reason"])
        history_id = store.latest_subgoal_history_id(session_id)
        if history_id is not None:
            store.set_subgoal_history_drift(
                history_id, verdict["severity"], verdict["reason"], json.dumps(verdict)
            )
    print(f"drift {verdict['severity']} for {session_id}")
    return 0


def cmd_ack_drift(args: argparse.Namespace) -> int:
    """Acknowledge (resolve) a flagged sub-goal drift so the blue dot clears."""
    session_id = resolve_session_id(_adapter(), args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        store.ack_drift(session_id)
    print(f"drift acknowledged for {session_id}")
    return 0


def cmd_assess_aim(args: argparse.Namespace) -> int:
    """Impartial "is the AIM fulfilled?" self-assessment (internal; spawned detached each turn).

    Judges the session's AIM holistically from a transcript evidence tail (sub-goals excluded)
    via a separate cheap ``claude -p`` — never the session agent — and stores the boolean verdict
    (the red DONE inside the progress bar). Eligibility, the new-turn gate and the stale-write
    guard live in ``aimmet.run_for_session`` so this path and the daemon fallback cannot diverge.
    """
    from . import aimmet

    cfg = config.load_config()
    if not cfg.assess_aim_on_turn:
        return 0
    adapter = _adapter()
    session_id = resolve_session_id(adapter, args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        session = store.get(session_id)
        if session is None:
            return 0
        verdict = aimmet.run_for_session(store, adapter, session, cfg)
    if verdict is not None:
        print(f"aim-met {verdict['met']} for {session_id}")
    return 0


def _subgoal_at(store: Store, session_id: str, position: int) -> Subgoal | None:
    """The session's *position*-th sub-goal (1-based), or None (with an error) if OOR.

    Sub-goals are addressed by their 1-based position in *this session's* checklist —
    NOT a global DB id — so ``ccc check 3`` can never silently tick a different
    session's item when several sessions share a working directory.
    """
    subs = store.list_subgoals(session_id)
    if 1 <= position <= len(subs):
        return subs[position - 1]
    plural = "" if len(subs) == 1 else "s"
    print(
        f"error: sub-goal position {position} out of range "
        f"(session has {len(subs)} sub-goal{plural})",
        file=sys.stderr,
    )
    return None


def cmd_check(args: argparse.Namespace) -> int:
    session_id = resolve_session_id(_adapter(), args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        target = _subgoal_at(store, session_id, args.position)
        if target is None:
            return 1
        store.set_subgoal_checked(target.id, not args.uncheck)
    state = "unchecked" if args.uncheck else "checked"
    print(f"sub-goal {args.position} {state}: {target.text}")
    return 0


def cmd_subgoal_check(args: argparse.Namespace) -> int:
    """Attach a shell predicate to a sub-goal (exit 0 auto-ticks it); empty clears it."""
    session_id = resolve_session_id(_adapter(), args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    with Store() as store:
        target = _subgoal_at(store, session_id, args.position)
        if target is None:
            return 1
        store.set_subgoal_check(target.id, args.command or None)
    detail = f"set to {args.command!r}" if args.command else "cleared"
    print(f"sub-goal {args.position} check {detail}")
    return 0


def cmd_mark_done(args: argparse.Namespace) -> int:
    adapter = _adapter()
    session_id = resolve_session_id(adapter, args.session, None)
    if not session_id:
        print("error: could not resolve a session (pass --session <id>)", file=sys.stderr)
        return 1
    close = getattr(args, "close", False)
    quiet = getattr(args, "quiet", False)
    ticked = 0
    with Store() as store:
        store.ensure(session_id, cwd=os.getcwd())
        session = store.get(session_id)
        store.update_fields(
            session_id,
            done=not args.undo,
            status=Status.DONE.value if not args.undo else Status.IDLE.value,
            done_at=now_ms() if not args.undo else 0,
        )
        # Done is the human's authoritative verdict — reconcile the progress bar to
        # 100% so it never reads "done but 2/5". Reopening (--undo) leaves ticks as-is.
        if not args.undo:
            ticked = store.check_all_subgoals(session_id)
            # A future-job draft can't be "done"-graded: archive its mirror file and
            # drop it out of the FUTURE list (archived) instead of leaving a done draft.
            if session is not None and session.draft:
                from . import futuresync

                cfg = config.load_config()
                if session.future_file:
                    futuresync.archive_file(store, cfg, session, "archived")
                store.update_fields(session_id, archived=True)
            # --close: arm a one-shot close-after-turn request the release-locks Stop
            # hook claims + fires (SIGTERM + pane/tab close). A headless/SDK run has no
            # real tab to close, so arming there is a no-op (stderr note; still done).
            if close:
                if _close_arming_suppressed():
                    print(
                        "note: --close ignored under a headless/SDK entrypoint "
                        "(CCC_INTERNAL / sdk) — nothing armed",
                        file=sys.stderr,
                    )
                else:
                    store.update_fields(session_id, close_requested_at=now_ms())
        else:
            # Reopening disarms any pending close request so a resumed session never
            # self-closes on a stale arm.
            store.update_fields(session_id, close_requested_at=0)
            if session is not None and session.draft and session.archived:
                # Un-doing a future-job draft: clear the archived flag and re-export its
                # mirror file so it reappears in both ccc's FUTURE list and the Obsidian
                # folder (inverse of the done-draft archive above).
                from . import futuresync

                cfg = config.load_config()
                store.update_fields(session_id, archived=False)
                refreshed = store.get(session_id)
                if refreshed is not None:
                    futuresync.unarchive_file(store, cfg, refreshed)
    if not quiet:
        suffix = (
            f" (ticked {ticked} remaining sub-goal{'s' if ticked != 1 else ''})" if ticked else ""
        )
        print(f"{session_id} marked {'not done' if args.undo else 'done'}{suffix}")
    _spawn_sync_mirrors(config.load_config())  # done⇄running mirror move
    return 0


def cmd_close_now(args: argparse.Namespace) -> int:
    """Internal: SIGTERM the session's Claude process, then close its terminal pane/tab.

    Spawned detached by the ``release-locks`` Stop hook once a ``mark-done --close`` request
    is claimed. Best-effort at every step and NEVER raises: it settles briefly (so the
    render + the rest of the Stop chain, incl. auto-commit, finish), reaps a FRESH matching
    Claude PID, then closes the hosting tmux pane (first match) or the iTerm pane/tab — the
    latter only on fresh evidence (a hook-supplied ``--iterm`` id, or a live PID reaped this
    run), never on stale-store-only evidence.
    """
    import signal
    import subprocess
    import time

    from . import terminal

    session_id = args.session
    if not session_id:
        return 0
    time.sleep(_CLOSE_NOW_SETTLE_SEC)
    reaped_pid = False
    # (1) Reap a fresh, matching, non-conflicting live Claude PID.
    try:
        for live in _adapter().discover():
            if live.session_id != session_id or not live.alive or live.conflict:
                continue
            if live.pid > 0:
                try:
                    os.kill(live.pid, signal.SIGTERM)
                    reaped_pid = True
                except OSError:
                    pass
            break
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        pass
    with Store() as store:
        session = store.get(session_id)
    # (2) Terminal close, first match wins. (a) tmux pane.
    try:
        located = terminal.tmux_pane_for_session(session_id)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        located = None
    if located is not None:
        _window_target, pane_id = located
        try:
            subprocess.run(
                ["tmux", "kill-pane", "-t", pane_id],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return 0
    # (b) iTerm: the hook-supplied fresh id, else the stored id ONLY when a live PID was
    # reaped this run (stale-evidence guard — never close on a store-only id).
    iterm = (getattr(args, "iterm", "") or "").strip()
    target = iterm or (
        session.iterm_session_id if reaped_pid and session and session.iterm_session_id else ""
    )
    if target:
        try:
            terminal.close_iterm_session(target)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            pass
    return 0


def cmd_keep(args: argparse.Namespace) -> int:
    return _set_field(args, "keep", not args.off)


def cmd_toggle_idle(args: argparse.Namespace) -> int:
    """Mute/unmute Claude Code's idle 'waiting for input' popups (the TUI `ti` chord).

    Flips ``agentPushNotifEnabled`` in Claude Code's settings.json. With --on/--off
    it forces the state; with neither it toggles. Prints the resulting state.
    """
    from . import idlenotify

    try:
        if args.on:
            idlenotify.set_enabled(True)
            state = True
        elif args.off:
            idlenotify.set_enabled(False)
            state = False
        else:
            state = idlenotify.toggle()
    except (OSError, ValueError) as exc:  # JSONDecodeError subclasses ValueError
        print(f"could not update {idlenotify.settings_path()}: {exc}", file=sys.stderr)
        return 1
    print(
        "idle popups ON — you'll be pinged when a session goes idle"
        if state
        else "idle popups OFF — 'waiting for input' notifications muted"
    )
    print("(restart an already-running session to pick up the change)", file=sys.stderr)
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    with Store() as store:
        session = store.get(args.session_id)
        # Deleting a file-mirrored future job: archive its Obsidian file first so the
        # capture is preserved and it leaves the live scan (never orphaned on disk).
        if session is not None and session.draft and session.future_file:
            from . import futuresync

            futuresync.archive_file(store, config.load_config(), session, "archived")
        store.delete(args.session_id)
    print(f"removed {args.session_id}")
    _spawn_sync_mirrors(config.load_config())  # drop any running/done mirror it left behind
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete leftover rows left by headless ``claude -p`` runs.

    Two kinds, never one that is live / done / kept: *contentless* rows (no aim,
    prompts, summary, next-step, sub-goals, importance, blocked/deadline) and
    *headless one-shots* — rows whose transcript is a ``claude -p`` one-shot (e.g.
    ``ai.py``'s commit-message generation) that inherited the launching session's
    AIM. ``--dry-run`` lists them without deleting.
    """
    from .core import headless_leak_ids, orphan_launched_ids
    from .models import humanize_age, short_id

    adapter = _adapter()
    live_ids = {ls.session_id for ls in adapter.discover()}
    with Store() as store:
        headless_ids = headless_leak_ids(store, adapter, live_ids)
        orphan_ids = orphan_launched_ids(store, adapter, live_ids)
        victims = store.prunable_sessions(
            protect_ids=live_ids, headless_ids=headless_ids, orphan_ids=orphan_ids
        )
        if not victims:
            print("nothing to prune — no leftover sessions")
            return 0
        for session in victims:
            cwd = session.cwd or "?"
            age = humanize_age(session.last_response_at)
            if session.session_id in headless_ids:
                tag = "headless"
            elif session.session_id in orphan_ids:
                tag = "orphan"
            else:
                tag = "empty"
            print(f"  {short_id(session.session_id)}  {cwd:<28}  {age:<6}  [{tag}]")
        if args.dry_run:
            print(f"[dry-run] would prune {len(victims)} session(s)")
            return 0
        deleted = store.delete_many(s.session_id for s in victims)
    print(f"pruned {deleted} leftover session(s)")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume a session in the current terminal (replaces this process).

    Fails closed on the multi-account billing risk (D8/R1): pins the session's own
    Claude account into the exec env, and refuses when the account is unknown, when the
    id is live under two accounts (D9), or when no transcript resolves under it.
    """
    from . import accounts, core

    with Store() as store:
        session = store.get(args.session_id)
    config_dir = session.config_dir if session else ""
    cwd = session.cwd if session else ""
    no_codex = bool(session.no_codex) if session else False
    # The three fail-closed checks (unknown account in multi-account mode, D9 conflict,
    # missing transcript) live in core so `ccc restore-snapshot` refuses identically.
    blockers = core.resume_blockers(args.session_id, cwd, config_dir, _adapter())
    if blockers:
        print(f"error: {blockers[0]}", file=sys.stderr)
        return 1
    # Terminal guard (same trap as `start-job`): this execs claude IN PLACE, so with no
    # TTY the resumed session would run headless for a single turn and exit instead of
    # opening for the user. Hand it to a real tab; refuse if none can be opened.
    if not has_terminal():
        from . import terminal

        if terminal.resume_in_new_tab(cwd, args.session_id, config_dir, no_codex=no_codex):
            print(f"no terminal here — resumed {args.session_id} in a new tab instead")
            return 0
        print(
            f"error: `ccc resume {args.session_id}` needs a terminal (it execs claude in "
            "place) and no tab could be opened. Run it from a terminal tab.",
            file=sys.stderr,
        )
        return 1
    if cwd and os.path.isdir(cwd):
        os.chdir(cwd)
    # Pin the session's account AND its own env flags (CCC_NO_CODEX) into os.environ,
    # then exec (D8) — os.execvp inherits the mutated environment.
    accounts.session_apply_to_environ(accounts.LaunchTarget(config_dir, no_codex))
    os.execvp("claude", ["claude", "--resume", args.session_id])  # replaces this process


def _account_config_dir(label: str | None) -> tuple[str, str | None]:
    """Resolve an account *label* to its absolute config dir.

    Returns ``(config_dir, error)``: an omitted/empty *label* ⇒ the routed job account
    (``routing.pick_job_account`` — the default account under the ``""`` policy, so
    single-account setups are unchanged), see :mod:`.routing`; a known label ⇒ its resolved
    dir; an unknown label ⇒ ``("", "error: …")`` listing the configured accounts.
    """
    label = (label or "").strip()
    if not label:
        from . import routing

        return routing.pick_job_account()[1], None
    dirs = config.claude_config_dirs()
    path = dirs.get(label)
    if path is None:
        known = ", ".join(sorted(dirs)) or "(none)"
        return "", f"error: unknown account {label!r} — configured accounts: {known}"
    return str(path), None


def cmd_park(args: argparse.Namespace) -> int:
    """``ccc park`` — register a ready prompt NOW, auto-launch it in THIS tab at reset.

    Thin dispatcher over :func:`park.run_park` (prompt capture, deterministic fire-time
    from the account's usage snapshot, persistent draft row, same-tab countdown waiter,
    launch through :func:`cmd_start_job`). See ``command_center/park.py``.
    """
    from . import park

    return park.run_park(args)


def cmd_fire_attached(args: argparse.Namespace) -> int:
    """internal: resume a session WITH its parked (attached) prompt.

    Runs in the fresh tab the daemon opened because the attached prompt's own
    session tab was gone at fire time (see ``daemon._deliver_attached_prompts``).
    Claims the delivery via the one-shot conditional ``fire_at → 0`` update — a
    second tab refuses instead of delivering twice — then pins the session's
    account and execs ``claude --resume <id> "<prompt>"`` in the session's cwd.
    On an exec failure the fire time is re-armed so the daemon retries.
    """
    import time as _time

    from . import accounts

    with Store() as store:
        session = store.get(args.session_id)
        if session is None or not (session.prompt or "").strip():
            print(f"error: no parked prompt on session {args.session_id}", file=sys.stderr)
            return 1
        if not store.claim_fire(args.session_id):
            print(
                f"error: parked prompt for {args.session_id} was already delivered "
                "by another launcher",
                file=sys.stderr,
            )
            return 1
        prompt = (session.prompt or "").strip()
        cwd = session.cwd
        launch = accounts.LaunchTarget(session.config_dir, bool(session.no_codex))
    if cwd and os.path.isdir(cwd):
        os.chdir(cwd)
    accounts.session_apply_to_environ(launch)
    try:
        os.execvp("claude", ["claude", "--resume", args.session_id, prompt])
    except OSError as exc:
        with Store() as store:  # undo the claim: re-arm so the daemon retries
            store.update_fields(args.session_id, fire_at=int(_time.time()) + 900)
        print(f"error: could not resume {args.session_id}: {exc}", file=sys.stderr)
        return 1
    return 0  # unreachable on success (execvp replaced the process)


def cmd_claim_fire(args: argparse.Namespace) -> int:
    """internal: one-shot claim of a session's armed parked prompt (prints it).

    Run by the session itself (the hook's armed-prompt notice tells it to) when the
    user asks to continue with the parked prompt NOW instead of waiting for the
    auto-fire: exit 0 = the caller owns delivery — the full prompt text is on
    stdout and the auto-fire is disarmed; exit 1 = nothing armed / already
    delivered. The same conditional update backs the daemon's delivery and
    ``fire-attached``, so the prompt can never run twice.
    """
    with Store() as store:
        session = store.get(args.session_id)
        prompt = (session.prompt or "").strip() if session is not None else ""
        if session is None or not prompt:
            print(f"error: no parked prompt on session {args.session_id}", file=sys.stderr)
            return 1
        if not store.claim_fire(args.session_id):
            print(
                f"error: parked prompt for {args.session_id} is not armed "
                "(already delivered or disarmed)",
                file=sys.stderr,
            )
            return 1
    print(prompt)
    return 0


# The machine-readable contract of `ccc new-job --json` / `ccc jobs --json`. Bump only
# on an incompatible change; additive fields keep version 1 (consumers read by key).
JSON_SCHEMA_VERSION = 1


def _new_job_json(session: Session, created: bool) -> str:
    """The single-line ``ccc new-job --json`` document for *session*."""
    from . import accounts

    return json.dumps(
        {
            "version": JSON_SCHEMA_VERSION,
            "session_id": session.session_id,
            "created": created,
            "account": accounts.effective_account_label(session.config_dir or ""),
            "no_codex": bool(session.no_codex),
        }
    )


def _idempotency_conflict(existing: Session, cwd: str, aim: str, no_codex: bool) -> str:
    """Why *existing* is not the job the caller just described, or ``""``.

    A key is a promise that the SAME request maps to the same job; a caller that reuses
    one for different work has a bug, and silently handing back the old job would hide
    it. ``aim`` is skipped while the winning creator has not written it yet (the row is
    claimed, its AIM lands microseconds later — see ``Store.claim_idempotency_key``).
    """
    diffs: list[str] = []
    if os.path.normpath(existing.cwd or "") != os.path.normpath(cwd):
        diffs.append(f"cwd {existing.cwd!r} != {cwd!r}")
    if existing.aim is not None and (existing.aim or "").strip() != aim:
        diffs.append(f"aim {existing.aim!r} != {aim!r}")
    if bool(existing.no_codex) != no_codex:
        diffs.append(f"no_codex {bool(existing.no_codex)} != {no_codex}")
    return "; ".join(diffs)


def cmd_new_job(  # pylint: disable=too-many-branches,too-many-return-statements
    args: argparse.Namespace,
) -> int:
    """Register a FUTURE job — a saved AIM + prompt, launched later with ``ccc start-job``.

    The job id is a fresh UUID so the eventual ``claude --session-id <id>`` reuses it and
    the AIM stored here carries over. ``--prompt`` defaults to the AIM when omitted.
    ``--account`` pins the Claude account the job will launch (bill) under.
    ``-N/--no-codex`` bans every Codex integration in the launched session (refused on a
    codex job type). ``-K/--idempotency-key`` makes registration create-or-retrieve, so a
    retrying automation never double-registers; ``-J/--json`` prints one machine-readable
    line and nothing else.
    """
    import uuid

    as_json = bool(getattr(args, "json", False))

    def say(message: str) -> None:
        """Human chatter — suppressed entirely in --json mode (stdout is the document)."""
        if not as_json:
            print(message)

    aim = (args.aim or "").strip()
    if not aim:
        print("error: --aim is required", file=sys.stderr)
        return 1
    job_type = getattr(args, "job_type", "claude") or "claude"
    no_codex = bool(getattr(args, "no_codex", False))
    conflict = no_codex_conflict(job_type, no_codex)
    if conflict:
        print(f"error: {conflict}", file=sys.stderr)
        return 2
    start_date = (getattr(args, "start_date", None) or "").strip() or None
    if start_date and parse_iso_date(start_date) is None:
        print(
            f"error: --start-date {start_date!r} is not a valid ISO date (YYYY-MM-DD)",
            file=sys.stderr,
        )
        return 1
    config_dir, err = _account_config_dir(getattr(args, "account", None))
    if err:
        print(err, file=sys.stderr)
        return 1
    cwd = args.cwd or os.getcwd()
    session_id = str(uuid.uuid4())
    idempotency_key = (getattr(args, "idempotency_key", None) or "").strip()
    # --at-reset: arm the job to auto-fire when the selected rate-limit window resets.
    # Deterministic (the window's resets_at + buffer, utilization is not an input) and
    # fail-loud: no usable reset time is a registration ERROR, never "fire now".
    fire_at = 0
    fire_window = ""
    if getattr(args, "at_reset", False):
        import time as _time

        from . import accounts as _accounts
        from . import park, usage

        size_err = park.prompt_size_error((args.prompt or aim).strip())
        if size_err:
            print(size_err, file=sys.stderr)
            return 1
        now = int(_time.time())
        window = getattr(args, "fire_window", None) or "five_hour"
        label = _accounts.effective_account_label(config_dir)
        snapshot = usage.read_usage(label)
        fire_opt = park.fire_time(snapshot, window, now)
        if fire_opt is None:
            snapshot = usage.fetch_claude_usage(label) or snapshot
            fire_opt = park.fire_time(snapshot, window, now)
        if fire_opt is None:
            print(
                f"error: --at-reset needs a usable {window} reset time for account "
                f"'{label}' — run one Claude turn there (the statusline captures usage) "
                "or check `ccc doctor`",
                file=sys.stderr,
            )
            return 1
        fire_at, fire_window = fire_opt, window
    with Store() as store:
        # Resolve the optional dependency (full UUID or a unique prefix / 4-hex hash).
        depends_on: str | None = None
        dep_ref = (getattr(args, "depends_on", None) or "").strip()
        if dep_ref:
            from . import deps

            try:
                depends_on = deps.resolve_dependency_ref(store, dep_ref)
            except deps.DependencyError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            if deps.would_create_cycle(store.get, session_id, depends_on):
                print(
                    f"error: depends-on {dep_ref!r} would create a dependency cycle",
                    file=sys.stderr,
                )
                return 1
        if idempotency_key:
            # Create-or-retrieve, atomically (one BEGIN IMMEDIATE in the store): concurrent
            # callers with the same key collapse onto ONE job, and a key reused for a
            # DIFFERENT job is a caller bug — refuse instead of returning the wrong id.
            # Claimed HERE, after every pre-flight check has passed, so a rejected
            # registration never leaves a half-built row owning the key.
            session_id, created = store.claim_idempotency_key(
                idempotency_key, session_id, cwd, no_codex=no_codex
            )
            if not created:
                existing = store.get(session_id)
                assert existing is not None  # the key's owner row always exists
                mismatch = _idempotency_conflict(existing, cwd, aim, no_codex)
                if mismatch:
                    print(
                        f"error: idempotency key {idempotency_key!r} already names job "
                        f"{session_id} with different parameters ({mismatch})",
                        file=sys.stderr,
                    )
                    return 2
                if as_json:
                    print(_new_job_json(existing, False))
                else:
                    print(f"future job already registered: {session_id}  ({existing.cwd})")
                    print(f"start it with:  ccc start-job {session_id}")
                return 0
        session = store.create_draft(
            session_id,
            cwd,
            aim,
            prompt=args.prompt,
            deadline=getattr(args, "deadline", None),
            start_when=getattr(args, "when", None),
            start_date=start_date,
            depends_on=depends_on,
            job_type=job_type,
            no_codex=no_codex,
            llm_overseer=getattr(args, "overseer", None) or DEFAULT_LLM,
            llm_exec=getattr(args, "executor", None) or DEFAULT_LLM,
            config_dir=config_dir,
            fire_at=fire_at,
            fire_window=fire_window,
            idempotency_key=idempotency_key,
        )
    if as_json:
        print(_new_job_json(session, True))
    else:
        print(f"future job created: {session_id}  ({cwd})")
        print(f"start it with:  ccc start-job {session_id}")
    cfg = config.load_config()
    if fire_at:
        from . import park, service

        say(f"armed: {park.format_fire(fire_at)} — the daemon dispatches within ~5 min after")
        if not service.is_installed(cfg):
            print(
                "warning: the ccc daemon service is not installed — the job will NOT "
                "auto-fire; run `ccc daemon --install`",
                file=sys.stderr,
            )
    from .spawn import spawn_ccc

    if cfg.aim_score_on_set:
        spawn_ccc(["score-aim", "--session", session_id])
    # Mirror the draft into its Obsidian markdown file out-of-band (never block the caller).
    if cfg.future_files:
        spawn_ccc(["sync-future"])
    return 0


def cmd_new_prompt(args: argparse.Namespace) -> int:
    """Create a fresh FUTURE-job capture file (a prefilled draft page), print its clickable path.

    A concurrency-safe, hash-named draft page under the future root: ``-r <cat>/<repo>`` places
    it in that repo's directory, otherwise it lands at the future root (the next sync moves it
    to the canonical dir). The persistent capture pad is created if missing. ``-o/--open`` also
    fires the ``obsidian://`` URI. The file is inert until its ``status`` is flipped to ``ready``.
    """
    import subprocess
    import uuid
    from datetime import date

    from . import futuresync, repos
    from .future_files import (
        display_hash,
        future_root,
        obsidian_uri,
        pad_path,
        parse_job_file,
        serialize,
    )
    from .links import osc8_link

    cfg = config.load_config()
    repo = (args.repo or "").strip()
    root = future_root(cfg)

    # Meta Bind repo options — every <cat>/<repo> under $GIT_BASE (same source the sync uses).
    repo_options: list[str] = []
    for category in repos.categories():
        repo_options.extend(f"{category}/{name}" for name in repos.repos_in(category))

    # Uniqueness: the 4-hex prefix must collide with no existing draft row and no file on disk.
    taken: set[str] = set()
    with Store() as store:
        taken.update(display_hash(s.session_id) for s in store.list_sessions() if s.draft)
    if root.exists():
        for path in root.rglob("*.md"):
            try:
                sid = parse_job_file(path.read_text(encoding="utf-8")).session_id
            except OSError:
                continue
            if sid.strip():
                taken.add(display_hash(sid))
    while True:
        session_id = str(uuid.uuid4())
        if display_hash(session_id) not in taken:
            break

    content = serialize(
        session_id=session_id,
        aim="",
        status="draft",
        repo=repo,
        job_type="claude",
        start_when="",
        start_date="",
        deadline="",
        created=date.today().isoformat(),
        prompt=None,
        repo_options=repo_options,
    )
    directory = root / repo if repo else root
    target = directory / f"new-job-{display_hash(session_id)}.md"
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    # Ensure the persistent capture pad exists (create it blank from the template if missing).
    if not pad_path(cfg).exists():
        futuresync.reset_pad(cfg)

    try:
        relpath = str(target.resolve().relative_to(futuresync.vault_root(cfg).resolve()))
    except (ValueError, OSError):
        relpath = str(target)
    uri = obsidian_uri(relpath)
    print(f"capture file created: {osc8_link(uri, str(target))}")
    if getattr(args, "open", False):
        try:
            # Detached: the opener must outlive this call (not a `with` block).
            subprocess.Popen(["open", uri])  # noqa: S603,S607  # pylint: disable=consider-using-with
        except OSError as exc:
            print(f"warning: could not open Obsidian: {exc}", file=sys.stderr)
    return 0


def has_terminal() -> bool:
    """True when this process is attached to a real terminal on BOTH stdin and stdout.

    ``start-job`` / ``resume`` replace themselves with ``claude`` via ``execvp``, and
    Claude Code only runs an INTERACTIVE session when it owns a TTY. With stdin
    redirected (a pipe, ``</dev/null``, an agent's background shell) the exec'd process
    instead consumes its argv prompt as a headless ONE-SHOT and exits after a single
    turn — see :func:`cmd_start_job`'s terminal guard for why that silently loses a job.

    ``CCC_START_JOB_HEADLESS=1`` forces True, for deliberate headless use and for the
    test suite (an autouse fixture in ``tests/conftest.py`` sets it, so the existing
    exec-path tests keep running without a pseudo-terminal).
    """
    if os.environ.get("CCC_START_JOB_HEADLESS"):
        return True
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):  # detached / closed / replaced streams
        return False


def cmd_start_job(  # pylint: disable=too-many-locals,too-many-branches
    args: argparse.Namespace,
) -> int:
    """Launch a saved future job: clear its draft flag, then exec ``claude --session-id``.

    Replaces this process with Claude Code (in the job's repo, AIM pre-set, prompt sent),
    so it is meant to run in its own tab (the TUI opens one via ``start_job_in_new_tab``).

    Launch safety for a file-mirrored draft: (a) a synchronous targeted import of its
    Obsidian file first, so any last-minute edit to the AIM / prompt / repo is picked up
    before launch; (b) clear the draft flag; (c) archive the file with a terminal
    ``launched`` status; (d) if the ``execvp`` itself fails, restore the draft + move the
    file back out of ``_archive/`` so nothing is silently lost.

    Guards + resume-awareness (decisions 13-14): the job must be a live draft
    (``draft=1 AND archived=0``) — validated BEFORE any state mutation. When Claude Code
    already has a transcript for this id in the project dir derived from the job's CURRENT
    cwd, launch is a BARE ``claude --resume <id> --model <id>`` (no prompt — the original
    prompt already ran, resume continues it); with no transcript it is the normal
    ``claude --session-id <id> "<prompt>"`` first-launch path. Both paths pass an explicit
    ``--effort`` (config ``launch_effort``, default xhigh; "" omits the flag).
    """
    from . import futuresync

    cfg = config.load_config()
    with Store() as store:
        resolved = _resolve_job_or_report(store, args.session_id)
        if resolved is None:
            return 1
        args.session_id = resolved  # full UUID from here on (store ops + --session-id argv)
        session = store.get(resolved)
        assert session is not None  # resolve_job_id only ever returns a real id
        # (0) Guards (decision 13): only a live draft is launchable — refuse BEFORE any
        # state mutation (or the targeted file import) so a bad call changes nothing.
        if not session.draft:
            print(
                f"error: {args.session_id} is not a future job (draft) — "
                "use `ccc resume` for a live/parked session",
                file=sys.stderr,
            )
            return 1
        if session.archived:
            print(f"error: job {args.session_id} is archived — cannot launch", file=sys.stderr)
            return 1
        # (0a) Terminal guard — a job ALWAYS starts in its own tab. This command execs
        # claude IN PLACE, so without a TTY the launched process gets no stdin, runs its
        # argv prompt as a headless ONE-SHOT and exits after a single turn. Its transcript
        # then opens with a ``queue-operation`` record, which is exactly the signature
        # ``ClaudeAdapter.is_oneshot_headless`` matches, so the daemon's ``prune_headless``
        # self-heal DELETES the row: the job ends up with no tab, no ccc row and no error
        # anywhere. That is how job 42fc3505 was lost on 2026-08-28 (an agent ran
        # ``ccc start-job -u`` inside a background shell instead of ``ccc open-job``).
        # So: no terminal → open a real tab and let THAT run the launch (inside it stdin is
        # a TTY, so this guard passes and the exec proceeds). Refuse outright if no tab can
        # be opened — never exec headless. Checked BEFORE any state mutation, so a refusal
        # leaves the draft and its file exactly as they were.
        if not has_terminal():
            from . import terminal

            if terminal.start_job_in_new_tab(
                args.session_id,
                force=getattr(args, "force", False),
                auto=getattr(args, "auto", False),
            ):
                print(
                    f"no terminal here — opened job {args.session_id} in a new tab instead "
                    "(a job must run in its own tab to stay visible in ccc)"
                )
                return 0
            if getattr(args, "auto", False):
                # Same contract as the other auto guards: disarm rather than let the
                # daemon retry a dispatch that cannot work, keeping the draft intact.
                store.update_fields(args.session_id, fire_at=0)
            print(
                f"error: `ccc start-job {args.session_id}` needs a terminal (it execs claude "
                "in place) and no tab could be opened. Run it from a terminal tab, or use "
                "`ccc open-job` which opens one for you. Set CCC_START_JOB_HEADLESS=1 only "
                "if you really want a headless one-shot (it will not appear in ccc).",
                file=sys.stderr,
            )
            return 1
        if session.future_file:  # (a) pull in last-minute file edits, then re-read the row
            abs_path = futuresync.vault_root(cfg) / session.future_file
            futuresync.run_sync(store, cfg, only_file=abs_path)
            refreshed = store.get(args.session_id)
            if refreshed is not None:
                session = refreshed
        # (0b) Premature-launch guard: a FIXED start date still ahead → warn and only
        # proceed on an explicit yes (checked AFTER the file import so a last-minute
        # start_date edit is honoured, and BEFORE any state mutation so "no" changes
        # nothing). The TUI's `r` runs this in the new tab too; when its ConfirmScreen
        # already asked it passes --force so the question is never asked twice.
        early = days_until_start(session)
        if early is not None and not getattr(args, "force", False):
            if getattr(args, "auto", False):
                # Unattended dispatch (daemon/park) NEVER bypasses the guard and never
                # loops a dead-tab retry: disarm the auto-fire, keep the draft intact.
                store.update_fields(args.session_id, fire_at=0)
                print(
                    f"error: auto-fire blocked — start date {session.start_date} not "
                    "reached; job disarmed (kept as draft), start it manually or re-arm",
                    file=sys.stderr,
                )
                return 1
            from datetime import date as _date

            plural = "s" if early != 1 else ""
            print(
                f"⚠ start date {session.start_date} not reached — {early} day{plural} early "
                f"(today {_date.today().isoformat()})."
            )
            if not sys.stdin.isatty():
                print(
                    "error: refusing to launch before the start date "
                    "(re-run with --force to override)",
                    file=sys.stderr,
                )
                return 1
            try:
                answer = input("Start this job anyway? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                print("not started — the job stays in SCHEDULED", file=sys.stderr)
                return 1
        # (0c) Dependency guard: an UNSATISFIED depends_on warns and only proceeds on an
        # explicit yes (after the file import so a last-minute dependency edit is honoured,
        # and BEFORE any state mutation). --force bypasses; non-TTY refuses. The TUI's `r`
        # already asked and passes --force so this never double-asks.
        from . import deps

        blocker = deps.launch_blocker(store, session)
        if blocker is not None and not getattr(args, "force", False):
            if getattr(args, "auto", False):
                store.update_fields(args.session_id, fire_at=0)
                print(
                    f"error: auto-fire blocked — depends on {blocker.parent_hash} "
                    f"({blocker.state}); job disarmed (kept as draft)",
                    file=sys.stderr,
                )
                return 1
            aim = (blocker.parent_aim or "?").strip()
            print(f'⚠ depends on {blocker.parent_hash} "{aim}" — {blocker.state}.')
            if not sys.stdin.isatty():
                print(
                    "error: refusing to launch a job with an unsatisfied dependency "
                    "(re-run with --force to override)",
                    file=sys.stderr,
                )
                return 1
            try:
                answer = input("Start this job anyway? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                print("not started — the dependency is not yet satisfied", file=sys.stderr)
                return 1
        had_file = bool(session.future_file)
        cwd = session.cwd
        config_dir = session.config_dir
        # Fail closed (D8/R1): a draft with no recorded account can't be launched safely
        # when several are configured. Refuse BEFORE any state mutation (draft flag / file
        # archive) so nothing is lost. Real drafts always carry an account (create_draft
        # stamps one; the migration backfills legacy rows), so this only trips on a
        # hand-corrupted row.
        from . import accounts

        if not config_dir and accounts.is_multi_account():
            if getattr(args, "auto", False):
                store.update_fields(args.session_id, fire_at=0)  # disarm, keep the draft
            print(
                f"error: job {args.session_id} has no recorded Claude account (config_dir) "
                "and several are configured — refusing to launch rather than risk billing "
                "the wrong account. Set its account via the TUI `e` form (/account row) or "
                "the job file's account select, or re-create it with `ccc new-job -A <label>`.",
                file=sys.stderr,
            )
            return 1
        prompt = (session.prompt or session.aim or "").strip()
        job_type = session.job_type or "claude"
        no_codex = bool(session.no_codex)
        # Second half of the domain rule (models.no_codex_conflict): a row that got both
        # settings anyway — a hand-edited DB, or a job file imported by an older ccc —
        # must not launch a Codex workflow with Codex banned. Refuse BEFORE claim_draft,
        # so the job survives to be fixed.
        conflict = no_codex_conflict(job_type, no_codex)
        if conflict:
            if getattr(args, "auto", False):
                store.update_fields(args.session_id, fire_at=0)  # disarm, keep the draft
            print(f"error: job {args.session_id}: {conflict}", file=sys.stderr)
            return 1
        overseer = session.llm_overseer if session.llm_overseer in LLM_MODEL_IDS else DEFAULT_LLM
        executor = session.llm_exec if session.llm_exec in LLM_MODEL_IDS else DEFAULT_LLM
        # (decision 14) Resume-aware: check ONLY the exact project dir derived from the
        # job's CURRENT cwd (the adapter munged-path convention) — not the glob fallback.
        resume = _cwd_transcript_exists(cwd, args.session_id, config_dir)
        # (b) promote ATOMICALLY: with several possible launchers (the park waiter, a
        # daemon dispatch tab, a manual start-job) exactly one may pass this line.
        if not store.claim_draft(args.session_id):
            print(
                f"error: job {args.session_id} was already claimed by another launcher — "
                "not starting it twice",
                file=sys.stderr,
            )
            return 1
        if had_file:  # (c) file leaves the live scan with a terminal status
            futuresync.archive_file(store, cfg, session, "launched")
    if cwd and os.path.isdir(cwd):
        os.chdir(cwd)
    if resume:
        # The original prompt was already submitted; a bare resume continues it (NO prompt
        # argument). --model + --resume is accepted by the CLI (smoke-checked). No delegation
        # prefix — that was applied on the first launch.
        argv = ["claude", "--resume", args.session_id, "--model", LLM_MODEL_IDS[overseer]]
    else:
        # First launch: the session runs ON the overseer's model, prompt sent.
        argv = ["claude", "--model", LLM_MODEL_IDS[overseer], "--session-id", args.session_id]
    # Explicit effort (both paths): the launched session's effort must not silently depend
    # on settings.json's effortLevel. --effort is accepted alongside any --model
    # (smoke-checked incl. haiku). "" omits the flag; an unknown value is ignored loudly.
    effort = str(getattr(cfg, "launch_effort", "") or "").strip().lower()
    if effort in EFFORT_LEVELS:
        argv += ["--effort", effort]
    elif effort:
        print(
            f"warning: launch_effort {effort!r} is not one of "
            f"{', '.join(EFFORT_LEVELS)} — flag omitted",
            file=sys.stderr,
        )
    if not resume and prompt:
        # An overseer/executor split (Claude jobs only): tell the overseer to delegate
        # implementation to Agent-tool subagents on the executor's model. Prepended before
        # the job_launch_prefix composition (a codex job launches into
        # /codex-implement-task-and-claude-review — Codex does the work, Claude verifies —
        # keeping the --model flag for the overseeing Claude side, but no delegation prefix).
        from .models import job_launch_prefix

        if job_type == "claude" and executor != overseer:
            prompt = (
                f"[orchestration] You are the overseer running as {overseer}. Delegate "
                f"implementation work to subagents via the Agent tool with model "
                f"'{LLM_AGENT_ALIAS[executor]}'; keep planning, review, verification and "
                f"integration yourself. " + prompt
            )
        argv.append(job_launch_prefix(job_type) + prompt)  # single argv element — no quoting
    _spawn_sync_mirrors(cfg)  # (BEFORE execvp: the success path replaces this process)
    # Pin the job's OWN Claude account (D8) into os.environ before exec — the default
    # account unsets CLAUDE_CONFIG_DIR, any other sets it — so an ambient value in this
    # tab can never bill the wrong seat — plus its own flags (CCC_NO_CODEX for a
    # -N/--no-codex job). os.execvp inherits the mutated environment.
    accounts.session_apply_to_environ(accounts.LaunchTarget(config_dir, no_codex))
    try:
        os.execvp("claude", argv)  # replaces this process on success
    except OSError as exc:  # (d) launch failed — undo the promotion so the job survives
        with Store() as store:
            store.update_fields(args.session_id, draft=True)
            if had_file:
                futuresync.unarchive_file(store, cfg, session)
        print(f"error: could not launch job {args.session_id}: {exc}", file=sys.stderr)
        return 1
    return 0  # unreachable on success (execvp replaced the process); keeps the type checker happy


def _cwd_transcript_exists(cwd: str, session_id: str, config_dir: str = "") -> bool:
    """Whether a transcript for *session_id* exists in the project dir for *cwd* only.

    The resume-aware launch check (decision 14): Claude Code stores a session's transcript
    at ``<account>/projects/<cwd-with-/-as->/<id>.jsonl``. We probe THAT exact path
    (derived from the job's current cwd), deliberately not the adapter's ``*/<id>.jsonl``
    glob fallback — a job whose cwd was edited must resume in its current repo, not wherever
    an old transcript happens to sit. *config_dir* selects the job's own account (the
    default account when "").
    """
    from pathlib import Path

    base = Path(config_dir).expanduser() if config_dir else config.claude_home()
    encoded = (cwd or "").replace("/", "-")
    return (base / "projects" / encoded / f"{session_id}.jsonl").exists()


def _resolve_job_or_report(store: Store, given: str) -> str | None:
    """Resolve *given* (full id or unique id prefix) to a full job id, reporting on failure.

    Wraps :func:`store.resolve_job_id`: on an ambiguous prefix prints the ``ambiguous job
    id`` line, on no match prints the standard ``no such job`` line — both to stderr — and
    returns ``None`` so the caller just ``return 1``. Otherwise returns the resolved id.
    """
    from .store import AmbiguousJobId, resolve_job_id

    try:
        resolved = resolve_job_id(store, given)
    except AmbiguousJobId as exc:
        print(str(exc), file=sys.stderr)
        return None
    if resolved is None:
        print(f"error: no such job {given}", file=sys.stderr)
        return None
    return resolved


def _job_target_id(args: argparse.Namespace) -> str:
    """The job UUID from a positional ``session_id`` or ``-f/--file`` (exactly one).

    ``--file`` reads the id from the markdown file's frontmatter — how every in-note
    Obsidian button (start / done / delete / restore) targets its own job. Returns ""
    after printing a clear stderr message on any miss, so callers just ``return 1``.
    """
    from pathlib import Path

    from . import future_files

    session_id = (getattr(args, "session_id", None) or "").strip()
    file_arg = getattr(args, "file", None)
    if file_arg:
        if session_id:
            print("error: give either a session_id or --file, not both", file=sys.stderr)
            return ""
        path = Path(os.path.abspath(os.path.expanduser(file_arg)))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read --file {file_arg}: {exc}", file=sys.stderr)
            return ""
        session_id = future_files.parse_job_file(text).session_id.strip()
        if not session_id:
            print(
                f"error: --file {file_arg} has no session_id (not a future-job file?)",
                file=sys.stderr,
            )
            return ""
    elif not session_id:
        print("error: need a session_id or --file PATH", file=sys.stderr)
        return ""
    return session_id


def cmd_done_job(args: argparse.Namespace) -> int:
    """Mark a FUTURE job (draft) done WITHOUT running it — it joins the DONE list/mirror.

    Distinct from ``mark-done`` on a draft (the *cancel* path: the draft stays a draft,
    is archived, and is never mirrored): done-job means "this got done", so the draft is
    promoted out of draft-hood (``draft=0, done=1``), its future file is archived with a
    terminal ``done`` status, sub-goals are reconciled to 100%, and the next mirror pass
    writes its final snapshot under ``done_dir`` (the Done Jobs dashboard).
    """
    from . import futuresync

    session_id = _job_target_id(args)
    if not session_id:
        return 1
    cfg = config.load_config()
    with Store() as store:
        resolved = _resolve_job_or_report(store, session_id)
        if resolved is None:
            return 1
        session_id = resolved
        session = store.get(session_id)
        assert session is not None  # resolve_job_id only ever returns a real id
        if not session.draft or session.archived:
            print(
                f"error: {session_id} is not a live future job — "
                "use `ccc mark-done` for a running session",
                file=sys.stderr,
            )
            return 1
        if session.future_file:
            futuresync.archive_file(store, cfg, session, "done")
        store.update_fields(
            session_id,
            draft=False,
            done=True,
            done_at=now_ms(),
            status=Status.DONE.value,
        )
        ticked = store.check_all_subgoals(session_id)
    suffix = f" (ticked {ticked} sub-goal{'s' if ticked != 1 else ''})" if ticked else ""
    print(f"future job {session_id} marked done{suffix} — moving to the DONE list")
    _spawn_sync_mirrors(cfg)  # writes the done/ mirror
    return 0


def cmd_delete_job(args: argparse.Namespace) -> int:
    """Move a FUTURE job (draft) to the vault's ``delete/`` trash (restorable).

    The row is soft-deleted (``archived=1`` — it leaves the FUTURE list and every sync
    scan) and its file moves to ``delete_dir/<cat>/<repo>/`` with ``status: deleted``, a
    ``deleted: <date>`` stamp and a single "↩ Stage job back in" button. Restore with
    ``ccc restore-job`` (or the button / the delete dashboard).
    """
    from . import futuresync

    session_id = _job_target_id(args)
    if not session_id:
        return 1
    cfg = config.load_config()
    with Store() as store:
        resolved = _resolve_job_or_report(store, session_id)
        if resolved is None:
            return 1
        session_id = resolved
        session = store.get(session_id)
        assert session is not None  # resolve_job_id only ever returns a real id
        if not session.draft or session.archived:
            print(
                f"error: {session_id} is not a live future job — "
                "use `ccc rm` for a tracked session",
                file=sys.stderr,
            )
            return 1
        dest = futuresync.delete_file(store, cfg, session)
        store.update_fields(session_id, archived=True)
    print(f"deleted future job {session_id} → {dest} (restore: ccc restore-job -f '{dest}')")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    """Soft-hide a PARKED session (``archived=1``) — ``delete-job``'s counterpart for a
    real (non-draft) session, added for tp's single-listing rule.

    tp imports ccc's parked jobs onto its own board and then retires them here so a job is
    listed once. The alternatives are wrong for that: ``rm`` hard-deletes the row and with
    it the cwd/account ``ccc resume`` needs (a multi-account setup then refuses to resume),
    ``mark-done`` puts a job nobody did on the Done dashboard, and ``delete-job`` accepts
    FUTURE drafts only. Archiving keeps the row — ``Store.get`` ignores ``archived`` — so
    ``ccc resume <id>`` / ``ccc resume-job <id>`` still work; the running mirror is dropped.
    A resumed session is un-archived the moment it is seen live
    (:meth:`Store.upsert_from_live`) so the TUI tracks it again; when it parks once more
    it is listed in ccc again until tp re-archives it (``tp import-ccc -a -z``).

    Refuses a LIVE session and a FUTURE draft (use ``delete-job``). ``-u/--undo`` clears
    the flag.
    """
    from . import mirrors

    cfg = config.load_config()
    with Store() as store:
        resolved = _resolve_job_or_report(store, args.session_id)
        if resolved is None:
            return 1
        session = store.get(resolved)
        assert session is not None  # resolve_job_id only ever returns a real id
        if args.undo:
            if not session.archived:
                print(f"error: {resolved} is not archived", file=sys.stderr)
                return 1
            store.update_fields(resolved, archived=False)
            print(f"unarchived {resolved}")
            _spawn_sync_mirrors(cfg)
            return 0
        if session.draft:
            print(
                f"error: {resolved} is a FUTURE job — trash it with `ccc delete-job` instead",
                file=sys.stderr,
            )
            return 1
        live_ids = {ls.session_id for ls in _adapter().discover() if ls.alive}
        if resolved in live_ids:
            print(f"error: {resolved} is live — park or exit it first", file=sys.stderr)
            return 1
        if session.archived:
            print(f"{resolved} is already archived")
            return 0
        store.update_fields(resolved, archived=True)
        mirrors.remove_mirror(cfg, resolved)
    print(
        f"archived {resolved} (still resumable: ccc resume {resolved}; "
        f"undo: ccc archive -u {resolved})"
    )
    return 0


def cmd_restore_job(args: argparse.Namespace) -> int:
    """Stage a deleted job back into FUTURE (inverse of ``delete-job``).

    Normal path: the row still exists (soft-deleted) — clear ``archived`` and move the
    file back to its canonical spot under ``future_dir`` (``status: registered``, live
    sync resumes). Fallback: the row was pruned — re-register it from the trashed file
    itself (``--file`` required), keeping the same UUID.
    """
    from . import futuresync, repos
    from .future_files import repo_to_cwd, validate
    from .store import AmbiguousJobId, resolve_job_id

    session_id = _job_target_id(args)
    if not session_id:
        return 1
    cfg = config.load_config()
    file_arg = getattr(args, "file", None)
    with Store() as store:
        # Resolve a unique id prefix (e.g. from `ccc jobs`) to the full UUID; a no-match
        # leaves session_id as-is so it flows to the re-register-from-file path below.
        try:
            resolved = resolve_job_id(store, session_id)
        except AmbiguousJobId as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if resolved is not None:
            session_id = resolved
        session = store.get(session_id)
        if session is not None and session.draft and not session.archived:
            print(f"error: {session_id} is already a live future job", file=sys.stderr)
            return 1
        if session is not None and session.draft:
            # fire_at=0: a restored job must never inherit a stale armed fire time —
            # an overdue one would silently auto-launch on the next daemon pass.
            store.update_fields(session_id, archived=False, done=False, done_at=0, fire_at=0)
            refreshed = store.get(session_id)
            assert refreshed is not None
            futuresync.unarchive_file(store, cfg, refreshed)
            print(f"restored future job {session_id} — staged back into FUTURE")
            return 0
        if session is not None:
            print(f"error: {session_id} is not a future job (draft)", file=sys.stderr)
            return 1
        # Row gone (pruned/removed): re-register from the trashed file, same UUID.
        if not file_arg:
            print(
                f"error: no such job {session_id} in the store — "
                "pass -f/--file <trashed file> to re-register it",
                file=sys.stderr,
            )
            return 1
        from pathlib import Path

        from . import accounts, future_files, routing

        path = Path(os.path.abspath(os.path.expanduser(file_arg)))
        job = future_files.parse_job_file(path.read_text(encoding="utf-8"))
        git_base = repos.git_base()
        problems = job.errors + validate(job, git_base)
        if problems:
            print("error: cannot re-register from file:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        store.create_draft(
            job.session_id,
            str(repo_to_cwd(job.repo, git_base)),
            job.aim,
            prompt=job.prompt,
            deadline=job.deadline or None,
            start_when=job.start_when or None,
            start_date=job.start_date or None,
            job_type=job.job_type or "claude",
            llm_overseer=job.llm_overseer,
            llm_exec=job.llm_exec,
            config_dir=(
                accounts.account_config_dir(getattr(job, "account", ""))
                or routing.pick_job_account()[1]
            ),
        )
        store.update_fields(job.session_id, future_file=str(path))
        refreshed = store.get(job.session_id)
        assert refreshed is not None
        futuresync.unarchive_file(store, cfg, refreshed)
    print(f"re-registered future job {session_id} from {file_arg} — staged back into FUTURE")
    return 0


def cmd_open_job(args: argparse.Namespace) -> int:
    """Open a saved future job in a new iTerm tab — the CLI twin of the TUI's ``r``.

    Accepts EITHER a positional ``session_id`` OR ``-f/--file PATH`` (exactly one; they are
    mutually exclusive). ``--file`` is how the in-note Obsidian "▶ Start this job" button
    launches a job: the button's Shell Commands entry passes the active file's absolute path,
    from which the ``session_id`` is read via :func:`future_files.parse_job_file`. Either way
    the rest is the exact same path the TUI uses for a draft (``terminal.start_job_in_new_tab``
    → a new login-shell tab running ``ccc start-job <id>``). Routing through a login shell
    means ``claude`` is always on PATH — so this works when invoked from a non-iTerm context
    (e.g. an Obsidian button): the AppleScript creates an iTerm window if none exists and
    types the command into the login shell, sidestepping the ``execvp("claude")`` ENOENT the
    old direct-exec Obsidian path hit.

    Validates the id is a real, un-archived draft; a clear error + exit 1 otherwise (including
    an unreadable/unparseable ``--file`` or one whose frontmatter has no ``session_id``).
    """
    from . import terminal

    session_id = _job_target_id(args)
    if not session_id:
        return 1

    with Store() as store:
        resolved = _resolve_job_or_report(store, session_id)
        if resolved is None:
            return 1
        session_id = resolved
        session = store.get(session_id)
        assert session is not None  # resolve_job_id only ever returns a real id
    if session.archived:
        print(f"error: job {session_id} is archived — cannot open", file=sys.stderr)
        return 1
    if not session.draft:
        print(
            f"error: {session_id} is not a future job (draft) — "
            "use `ccc resume` for a live/parked session",
            file=sys.stderr,
        )
        return 1
    if terminal.start_job_in_new_tab(session_id):
        print(f"opening future job {session_id} in a new tab")
        return 0
    print(
        f"error: could not open a terminal tab — run: ccc start-job {session_id}",
        file=sys.stderr,
    )
    return 1


def cmd_jobs(args: argparse.Namespace) -> int:
    """List registered future jobs (drafts), newest first.

    ``-j/--json`` prints ONE machine-readable document instead (schema
    :data:`JSON_SCHEMA_VERSION`) — no prompts, no colour, nothing else on stdout — so an
    automation can enumerate parked work without scraping the human table.
    """
    from . import accounts, colors, park
    from .models import short_id

    multi = accounts.is_multi_account()
    with Store() as store:
        drafts = sorted((s for s in store.list_sessions() if s.draft), key=lambda s: -s.created_at)
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "version": JSON_SCHEMA_VERSION,
                    "jobs": [
                        {
                            "session_id": s.session_id,
                            "cwd": s.cwd,
                            "aim": s.aim or "",
                            "draft": bool(s.draft),
                            "archived": bool(s.archived),
                            "created_at": s.created_at,
                            "account": accounts.account_label(s.config_dir or ""),
                            "no_codex": bool(s.no_codex),
                            "idempotency_key": s.idempotency_key or None,
                        }
                        for s in drafts
                    ],
                }
            )
        )
        return 0
    if not drafts:
        print("no future jobs — create one with `ccc new-job` or the `fn` chord in the TUI")
        return 0
    for session in drafts:
        folder = colors.short_folder(session.cwd)
        tag = "" if session.job_type == "claude" else f"  [{session.job_type}]"
        if session.no_codex:  # the per-job Codex ban (CCC_NO_CODEX=1 at launch)
            tag += "  [no-codex]"
        # In multi-account mode, tag a non-default account (e.g. [work]) like the codex tag.
        if multi and not accounts.is_default_config_dir(session.config_dir or ""):
            tag += f"  [{accounts.account_label(session.config_dir or '')}]"
        when = f"  [starts {session.start_date}]" if session.start_date else ""
        if session.fire_at:
            when += f"  [⏳ {park.format_fire(session.fire_at)}]"
        print(f"  {short_id(session.session_id)}  {folder:<30}  {session.aim or '—'}{tag}{when}")
    return 0


def cmd_job_account(args: argparse.Namespace) -> int:
    """Show each account's usage urgency and which account a NEW job will bill to.

    One aligned row per configured account — its Fable weekly used%, when that window
    resets, the ``(100-used%)/hours-to-reset`` burn rate the ``"auto"`` policy ranks by,
    and any reason it is unusable (no data / stale / dead / exhausted) — with a ``← pick``
    marker on the row :func:`routing.pick_job_account` currently selects. A trailing line
    spells out the active ``job_account`` policy and the account it resolves to. Read-only;
    always exits 0.

    ``-p/--pick`` prints ONLY the picked account label (one word, no report) — the
    machine-readable form shell wrappers dispatch on (the ``c()`` launcher resolves its
    account per invocation this way, so a long-lived shell never goes stale).
    """
    import time

    from . import accounts, routing, usage

    now = int(time.time())
    if getattr(args, "pick", False):
        print(routing.pick_job_account(now)[0])
        return 0
    scores = routing.score_accounts(now)
    _pick_label, pick_dir = routing.pick_job_account(now)
    print(f"  {'account':<10} {'used':>5}  {'reset':<16} {'urgency':>9}  note")
    for score in scores:
        used = f"{score.used_pct:.0f}%" if score.used_pct is not None else "—"
        reset = usage.format_reset(score.resets_at, now) if score.resets_at is not None else "—"
        urgency = f"{score.urgency:.2f}%/h" if score.urgency is not None else "—"
        note = "exhausted" if score.exhausted else ("" if score.note == "ok" else score.note)
        marker = " ← pick" if accounts.same_config_dir(score.config_dir, pick_dir) else ""
        print(f"  {score.label:<10} {used:>5}  {reset:<16} {urgency:>9}  {note}{marker}")
    policy = config.load_config().job_account
    print(f'policy: job_account = "{policy}" -> new jobs bill to: {_pick_label}')
    return 0


_QUOTA_MARK = {"available": "✅", "blocked": "⛔", "unknown": "❔", "disabled": "🚫"}


def cmd_quota(args: argparse.Namespace) -> int:  # pylint: disable=too-many-branches
    """Fast, cache-first quota oracle — which provider/account still has tokens.

    Reads only the snapshots ccc already maintains (~70 ms, no network) so any agent or
    script can consult it before spending a provider attempt. ``-r/--refresh`` is the sole
    networked path. ``-m/--mark`` records an authoritative rejection (e.g. a Copilot 429
    with its ``Retry-After``) so the NEXT check is instant and no doomed retry is made.

    Exit codes serve ``-p/--provider``: 0 available, 1 blocked, 2 unknown/disabled. The
    full report always exits 0 — it is a status view, not a gate.
    """
    import time

    from . import quota, usage

    now = int(time.time())

    if args.mark:
        if (args.retry_after is None) == (not args.until):
            print(
                "quota: -m/--mark requires exactly one of -u/--retry-after SEC or -U/--until STAMP",
                file=sys.stderr,
            )
            return 2
        if args.until:
            # Local-time ISO stamp; the deadline is EXCLUSIVE ("blocked while now <
            # stamp"), matching the store's `blocked_until > now` liveness test — so a
            # seat held "until Sunday night" frees at Monday 00:00:00, not 23:59:00.
            from datetime import datetime

            try:
                blocked_until = int(datetime.fromisoformat(args.until).timestamp())
            except ValueError:
                print(
                    f"quota: -U/--until must be an ISO stamp "
                    f"(e.g. 2026-09-07T00:00), got {args.until!r}",
                    file=sys.stderr,
                )
                return 2
        else:
            blocked_until = now + max(0, args.retry_after)
        kind = quota.KIND_HOLD if args.hold else quota.KIND_OBSERVED
        entry = quota.record_block(
            args.mark,
            blocked_until=blocked_until,
            reason=args.reason
            or ("administrative hold" if args.hold else "provider rejected the request"),
            source="cli",
            observed_at=now,
            kind=kind,
            scope=args.scope,
        )
        if entry.get("kind") == quota.KIND_HOLD and kind != quota.KIND_HOLD:
            print(f"quota: {args.mark} has an unexpired HOLD — observed mark not applied")
            return 0
        until = usage.format_reset(int(entry["blocked_until"]), now)
        word = "held" if entry.get("kind") == quota.KIND_HOLD else "blocked"
        print(f"quota: {args.mark} {word}, unblocks {until}")
        return 0

    if args.clear:
        cleared = quota.clear_block(args.clear, observed_only=args.observed_only)
        outcome = "cleared" if cleared else "not blocked"
        if not cleared and args.observed_only:
            outcome = "not cleared (no observed block; holds need a plain -c)"
        print(f"quota: {args.clear} {outcome}")
        return 0

    if args.refresh:
        for label in config.claude_config_dirs():
            usage.fetch_claude_usage(label, now)
        usage.fetch_copilot_usage(now)
        # Opt-in (a network call per Codex login), so gated exactly like the daemon's pass.
        if config.load_config().codex_usage:
            for home in config.codex_homes().values():
                usage.fetch_codex_usage(home, now)

    snap = quota.snapshot(model=args.model, now=now)

    if args.provider:
        match = next((p for p in snap["providers"] if p["id"] == args.provider), None)
        if match is None:
            print(f"quota: unknown provider '{args.provider}'", file=sys.stderr)
            return quota.EXIT_UNKNOWN
        if not args.json:
            reason = f" — {match['reason']}" if match.get("reason") else ""
            print(f"{match['id']}: {match['state']}{reason}")
        else:
            print(json.dumps(match, indent=2))
        return {
            quota.AVAILABLE: quota.EXIT_AVAILABLE,
            quota.BLOCKED: quota.EXIT_BLOCKED,
        }.get(match["state"], quota.EXIT_UNKNOWN)

    if args.best:
        print(snap["best_claude_account"].removeprefix("claude:"))
        return 0

    if args.json:
        print(json.dumps(snap, indent=2))
        return 0

    print(f"  {'provider':<16} {'state':<10} {'data age':<12} {'unblocks':<16} windows")
    for prov in snap["providers"]:
        state = prov["state"]
        unblocks = (
            usage.format_reset(prov["resets_at"], now)
            if prov.get("resets_at")
            else ("—" if state != quota.AVAILABLE else "")
        )
        # How old the evidence behind this verdict is. Cooldown/hold rows are labelled
        # as the age of the MARK, not of quota data — a hold recorded yesterday says
        # nothing about yesterday's usage figures.
        captured = int(prov.get("captured_at", 0) or 0)
        if not captured:
            age = "—"
        elif prov.get("source") in ("cooldown", "hold"):
            age = f"marked {usage._format_age(max(0, now - captured))}"  # noqa: SLF001
        else:
            age = usage._format_age(max(0, now - captured))  # noqa: SLF001
        wins = " ".join(
            f"{name.replace('_', '')} {win['used_pct']:.0f}%{'(stale)' if win.get('stale') else ''}"
            for name, win in (prov.get("windows") or {}).items()
        )
        mark = _QUOTA_MARK.get(state, " ")
        detail = wins or prov.get("reason", "")
        # A non-available provider that also has windows used to show ONLY the windows,
        # hiding why it is blocked — and a refusal's windows read as healthy headroom.
        if wins and state != quota.AVAILABLE and prov.get("reason"):
            detail = f"{prov['reason']} ({wins})"
        if prov.get("email"):
            detail = f"{detail}  [{prov['email']}]" if detail else f"[{prov['email']}]"
        print(f"  {mark} {prov['id']:<14} {state:<10} {age:<12} {unblocks:<16} {detail}")
    if snap["best_claude_account"]:
        print(f"best Claude account (spends what resets soonest): {snap['best_claude_account']}")
    best_codex = snap.get("best_codex_account", "")
    pin = snap.get("codex_pin") or {}
    pin_note = f"  [pin: {pin['account']} until {pin.get('until') or '∞'}]" if pin else ""
    if best_codex:
        print(f"codex seat for delegation (pin/holds-aware): {best_codex}{pin_note}")
    else:
        print(f"codex seat for delegation: none eligible (holds/blocks){pin_note}")
    return 0


def cmd_resume_halted(args: argparse.Namespace) -> int:
    """Auto-resume session-limit-halted sessions once the Claude limit resets.

    Default: run one orchestration tick and print the plan. ``--watch`` runs the
    flock-singleton poll loop (the real orchestrator the daemon spawns). ``--dry-run``
    prints candidates + planned actions without reaping or launching anything.
    """
    from . import resume

    cfg = config.load_config()
    if getattr(args, "stagger", 0):
        cfg.resume_stagger_sec = args.stagger
    if args.watch:
        return resume.watch(cfg)
    resume.tick(cfg, dry_run=args.dry_run)
    return 0


def cmd_sync_future(args: argparse.Namespace) -> int:
    """Reconcile FUTURE-job draft rows with their Obsidian markdown files (internal).

    Two-sided: exports fileless drafts, imports file edits (file wins), registers ``ready``
    files/pad, and archives drafts whose file has been gone past the delete grace. Idempotent
    and flock-guarded — safe to fire from the launchd WatchPaths trigger, the daemon and by
    hand. ``--file`` runs a targeted single-file import (used by ``start-job``).
    """
    from pathlib import Path

    from . import futuresync

    cfg = config.load_config()
    only_file = None
    if getattr(args, "file", None):
        only_file = Path(os.path.abspath(os.path.expanduser(args.file)))
    try:
        with Store() as store:
            report = futuresync.run_sync(store, cfg, only_file=only_file)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"error: sync-future failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"sync-future: exported={len(report.exported)} imported={len(report.imported)} "
        f"registered={len(report.registered)} errors={len(report.errors)} "
        f"archived={len(report.archived)}"
    )
    if getattr(args, "verbose", False):
        for detail in report.details:
            print(f"  {detail}")
    return 0


def cmd_sync_mirrors(args: argparse.Namespace) -> int:
    """Reconcile the RUNNING + DONE session mirrors with the store (internal, export-only).

    Writes ``running_dir``/``done_dir`` markdown mirrors for every active / finished
    session, byte-stable (writes only on a real change) and ``ccc_mirror``-guarded (only
    ever touches its own generated files). Idempotent + flock-guarded — safe to fire from
    the daemon and every lifecycle command. ``-v`` prints the per-item detail log.
    """
    from . import mirrors

    cfg = config.load_config()
    try:
        with Store() as store:
            report = mirrors.run_mirrors(store, cfg)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"error: sync-mirrors failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"sync-mirrors: running={len(report.running)} done={len(report.done)} "
        f"sessions={len(report.sessions)} "
        f"written={len(report.written)} removed={len(report.removed)}"
    )
    if getattr(args, "verbose", False):
        for sid in report.written:
            print(f"  wrote  {sid}")
        for path in report.removed:
            print(f"  removed {path}")
        for detail in report.details:
            print(f"  {detail}")
    return 0


def cmd_focus_job(args: argparse.Namespace) -> int:
    """Bring a LIVE session's iTerm tab to the front (decision 11).

    Verifies the session is in the live registry first — fresh adapter info, mirroring the
    TUI's live branch, NOT the stored iterm id alone — then focuses its tab. A dead / parked
    / auto-closed session gets a clear error + exit 1 (resume it from ccc instead).
    """
    from . import terminal

    adapter = _adapter()
    live_ids = {ls.session_id for ls in adapter.discover() if ls.alive}
    with Store() as store:
        session = store.get(args.session_id)
    if session is None:
        print(f"error: no such session {args.session_id}", file=sys.stderr)
        return 1
    if args.session_id not in live_ids:
        print(
            f"error: {args.session_id} is not live (its tab is gone) — resume it from ccc",
            file=sys.stderr,
        )
        return 1
    if session.iterm_session_id and terminal.focus_iterm_session(session.iterm_session_id):
        print(f"focused live tab: {args.session_id}")
        return 0
    if terminal.focus_tmux_window(args.session_id):
        print(f"focused tmux window: {args.session_id}")
        return 0
    print(
        f"error: {args.session_id} is live but its iTerm tab / tmux window can't be "
        "located — switch to it manually",
        file=sys.stderr,
    )
    return 1


def cmd_resume_job(args: argparse.Namespace) -> int:  # pylint: disable=too-many-return-statements
    """Resume a PARKED session in a NEW iTerm tab — the parked dashboard's ▶ path.

    The shell-invokable equivalent of pressing ``r`` in the TUI on a parked row (moon ☾):
    the session's process is gone but its transcript is on disk, so ``claude --resume <id>``
    in a fresh tab is safe. Invoked by the Obsidian parked-dashboard's ▶ button, which
    ``spawn``s ``ccc`` (never execs), so — unlike ``ccc resume`` — this NEVER replaces the
    current process; it always opens a new tab.

    Behaviour (mirrors the TUI ``action_resume``):
      * unknown / draft session → error + exit 1 (a draft is a FUTURE job — launch it with
        ``ccc open-job <id>`` instead);
      * LIVE session (in the fresh registry) → never a second REPL on the same transcript;
        focus the existing tab like ``ccc focus-job`` does;
      * no cwd / no recorded conversation → error + exit 1 (it never had a turn, so it can't
        be resumed);
      * otherwise open a new iTerm tab (or tmux window) running ``claude --resume <id>``.
    """
    from . import terminal

    adapter = _adapter()
    live_ids = {ls.session_id for ls in adapter.discover() if ls.alive}
    with Store() as store:
        session = store.get(args.session_id)
    if session is None:
        print(f"error: no such session {args.session_id}", file=sys.stderr)
        return 1
    if session.draft:
        print(
            f"error: {args.session_id} is a FUTURE job — launch it with "
            f"`ccc open-job {args.session_id}` instead",
            file=sys.stderr,
        )
        return 1
    if args.session_id in live_ids:
        # Live: don't open a second REPL on the same transcript — focus the existing tab.
        if session.iterm_session_id and terminal.focus_iterm_session(session.iterm_session_id):
            print(f"focused live tab: {args.session_id}")
            return 0
        if terminal.focus_tmux_window(args.session_id):
            print(f"focused tmux window: {args.session_id}")
            return 0
        print(
            f"error: {args.session_id} is live but its iTerm tab / tmux window can't be "
            "located — switch to it manually",
            file=sys.stderr,
        )
        return 1
    if not session.cwd:
        print(
            f"error: {args.session_id} has no working directory — cannot resume",
            file=sys.stderr,
        )
        return 1
    # Fail closed exactly like cmd_resume (D8/R1): this is the Obsidian parked-dashboard
    # ▶ button, so an unknown account must refuse rather than silently bill the default.
    from . import accounts

    if not session.config_dir and accounts.is_multi_account():
        print(
            f"error: {args.session_id} has no recorded Claude account (config_dir) and "
            "several are configured — refusing to resume rather than risk billing the "
            "wrong account. Start a turn from the intended account, then retry.",
            file=sys.stderr,
        )
        return 1
    if adapter.transcript_path(session.cwd, args.session_id, session.config_dir) is None:
        print(
            f"error: no recorded conversation for {args.session_id} — it never had a turn "
            "(or its transcript was deleted), so it cannot be resumed.",
            file=sys.stderr,
        )
        return 1
    if terminal.resume_in_new_tab(
        session.cwd, args.session_id, session.config_dir, no_codex=bool(session.no_codex)
    ):
        print(f"resuming in a new tab: {args.session_id}")
        return 0
    print(
        f"error: could not open a new tab — run `claude --resume {args.session_id}` manually",
        file=sys.stderr,
    )
    return 1


def unlaunch_job(
    store: Store, cfg: config.Config, session_id: str, live_ids: set[str]
) -> tuple[bool, str]:
    """Bring a launched job back to FUTURE (draft); return ``(ok, message)`` (decision 12).

    The pure state-logic core shared by ``ccc unlaunch`` (CLI) and the TUI dead-row dialog:
    no printing, and it takes an already-open *store* plus the caller's live-id set.
    Guards: (a) NO live process (id in *live_ids*) — kill/exit the tab first;
    (b) launched-draft provenance — a ``future_file`` on the row OR an archived job file for
    the UUID (refuse otherwise, so a hand-typed or genuinely-live session id can't be
    demoted); (c) ``done=0``. On success: restore ``draft=1`` + ``status=parked``, move the
    job file back out of ``future/_archive/`` (fresh export if the archive copy is gone), and
    remove the running mirror. Any transcript is preserved untouched, so a later ``start-job``
    resumes it (decision 14). The caller runs ``_spawn_sync_mirrors`` afterwards.
    """
    from . import futuresync, mirrors

    session = store.get(session_id)
    if session is None:
        return False, f"no such session {session_id}"
    if session_id in live_ids:  # (a)
        return False, f"{session_id} is still live — kill or exit its tab first"
    if session.done:  # (c)
        return False, f"{session_id} is done — reopen it with `ccc mark-done --undo` first"
    archived_file = None if session.future_file else _archived_job_file(cfg, session_id)
    if not session.future_file and archived_file is None:  # (b)
        return False, (
            f"{session_id} has no launched-draft provenance (no future-job file) — "
            "refusing to unlaunch"
        )
    # Restore the future-job state, then the file, then drop the running mirror.
    if archived_file is not None:  # future_file was cleared — point it back at the archive
        store.update_fields(
            session_id,
            future_file=futuresync._vault_relpath(cfg, archived_file),  # pylint: disable=protected-access
        )
    # fire_at=0 defensively: an unlaunched job must be re-armed explicitly, never
    # auto-fire off a leftover timestamp.
    store.update_fields(session_id, draft=True, status=Status.PARKED.value, fire_at=0)
    refreshed = store.get(session_id)
    if refreshed is not None:
        futuresync.unarchive_file(store, cfg, refreshed)  # archive → live (fresh export if gone)
    mirrors.remove_mirror(cfg, session_id)  # drop its running mirror now
    return True, f"unlaunched {session_id} → future job (draft)"


def cmd_unlaunch(args: argparse.Namespace) -> int:
    """Bring a launched job back to FUTURE (draft) so it can be re-launched (decision 12).

    Thin CLI wrapper over :func:`unlaunch_job`: takes a fresh live-registry snapshot, opens
    the store, and prints the ``(ok, message)`` result. See that function for the guards +
    action.
    """
    cfg = config.load_config()
    adapter = _adapter()
    live_ids = {ls.session_id for ls in adapter.discover() if ls.alive}
    with Store() as store:
        ok, msg = unlaunch_job(store, cfg, args.session_id, live_ids)
    if not ok:
        print(f"error: {msg}", file=sys.stderr)
        return 1
    _spawn_sync_mirrors(cfg)
    print(msg)
    return 0


def _archived_job_file(cfg: config.Config, session_id: str):  # -> Path | None
    """The ``future/_archive/`` job file whose frontmatter ``session_id`` matches, or None.

    Provenance fallback for ``ccc unlaunch`` when the row's ``future_file`` was cleared: a
    launched draft left a copy of its file in the archive, so its presence proves the id was
    a real future job (not a hand-typed / live session id).
    """
    from . import futuresync
    from .future_files import parse_job_file

    archive_dir = futuresync.future_root(cfg) / "_archive"
    if not archive_dir.is_dir():
        return None
    for path in sorted(archive_dir.glob("*.md")):
        try:
            if parse_job_file(path.read_text(encoding="utf-8")).session_id.strip() == session_id:
                return path
        except OSError:
            continue
    return None


def cmd_hook(args: argparse.Namespace) -> int:
    from .hooks import dispatch

    return dispatch(args.event)


def cmd_install_hooks(args: argparse.Namespace) -> int:
    from . import install

    return install.install_hooks(dry_run=args.dry_run, uninstall=args.uninstall)


def cmd_install_statusline(args: argparse.Namespace) -> int:
    from . import install

    return install.install_statusline(
        chain=args.chain, dry_run=args.dry_run, uninstall=args.uninstall
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    return doctor.run()


def cmd_install_commands(args: argparse.Namespace) -> int:
    from . import install_commands

    return install_commands.run(codex=args.codex, dry_run=args.dry_run, uninstall=args.uninstall)


def cmd_install_shell(args: argparse.Namespace) -> int:
    from . import shell_install

    return shell_install.install(
        rc_path=args.rc,
        shell=args.shell,
        wrapper_name=args.wrapper_name,
        include_wrapper=not args.no_wrapper,
        include_badges=not args.no_badges,
        dry_run=args.dry_run,
        uninstall=args.uninstall,
    )


def cmd_obsidian_setup(args: argparse.Namespace) -> int:
    from . import obsidian

    return obsidian.run_setup(
        root=args.root,
        dry_run=args.dry_run,
        uninstall=args.uninstall,
        install_plugins=args.install_plugins,
        yes=args.yes,
    )


def cmd_init(args: argparse.Namespace) -> int:
    from . import wizard

    return wizard.run(args)


def _render_todos_line(
    todos: list[tuple[str, str]], green: str, dim: str, reset: str, status_color: dict[str, str]
) -> str:
    """One-line TodoWrite strip: ``done/total`` counter + each item's box & short label.

    Truncates to a fixed budget so it never sprawls across the status line.
    """
    from .models import todo_box, todos_counts

    done_n, total_n = todos_counts(todos)
    cnt_color = status_color["done"] if done_n == total_n else green
    segs: list[str] = []
    used = 0
    for todo_status, subject in todos:
        label = subject if len(subject) <= 22 else subject[:21] + "…"
        seg = f"{todo_box(todo_status)} {label}"
        if used + len(seg) + 2 > 110:
            segs.append(f"{dim}…{reset}")
            break
        if todo_status == "completed":
            seg_color = status_color["done"]
        elif "progress" in todo_status:
            seg_color = (
                "\033[38;5;214m"  # amber: in-progress (distinct from red 'waiting for input')
            )
        else:
            seg_color = dim
        segs.append(f"{seg_color}{seg}{reset}")
        used += len(seg) + 2
    return f"{cnt_color}{done_n}/{total_n}{reset}  " + "  ".join(segs)


def _account_from_env() -> str | None:
    """The Claude account label for the CURRENT statusline capture, or ``None``.

    The statusline runs as a child of ``claude``, which exports ``CLAUDE_CONFIG_DIR``
    in its environment whenever it is set; we match that (resolved) path against
    :func:`config.claude_config_dirs`. When the env var is ABSENT the session is running
    under the DEFAULT account: by ``accounts.py``'s own invariant the default account
    runs with ``CLAUDE_CONFIG_DIR`` UNSET (setting it would change the Keychain service
    and de-authenticate), so an unset env unambiguously IS the default (first configured)
    label — no guessing. Returning the first label fixes a bug: once a *second* account
    was added the old "only if exactly one account" rule made private sessions NEVER
    write their usage snapshot (usage.json froze). A set env matching no configured
    account still returns ``None`` (the caller skips the write rather than contaminate a
    card with an unknown account's quota).
    """
    from pathlib import Path

    dirs = config.claude_config_dirs()
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        try:
            target = Path(env).expanduser().resolve()
        except OSError:
            return None
        for label, path in dirs.items():
            try:
                if path.resolve() == target:
                    return label
            except OSError:
                continue
        return None
    return next(iter(dirs), None)


def _read_statusline_stdin() -> dict | None:
    """Read + parse the status-line JSON object piped on stdin (None on any failure).

    Claude Code pipes its full status-line JSON (``session_id`` / ``cwd`` / ``model`` /
    ``rate_limits`` …) to the statusLine command. stdin can only be read once, so
    :func:`cmd_statusline` reads it a single time and reuses the parsed payload for BOTH
    usage capture and — when no ``--session`` was passed — deriving the session id.
    Best-effort and silent: it must never break the status line.
    """
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _capture_usage_from_payload(data: dict) -> None:
    """Persist the account-wide ``rate_limits`` (+ effort) from a parsed status-line payload.

    ``rate_limits`` is the only place this subscription-usage data is exposed. The
    write is routed to the account named by :func:`_account_from_env`, and SKIPPED
    entirely when that is ``None`` (never guess the default). Silent on every error —
    the status line must never break.
    """
    from . import usage

    account = _account_from_env()
    if account is not None:
        try:
            usage.write_usage(data.get("rate_limits"), account=account)
        except OSError:
            pass
    _capture_effort_from_payload(data)


def _effort_from_statusline_payload(data: dict) -> str | None:
    """Best-effort extraction of a reasoning-effort level from the status-line JSON.

    Checks the top-level keys Claude Code might expose (``effort`` / ``effortLevel`` /
    ``reasoningEffort``) and, when ``model`` is a nested object, its ``effort`` /
    ``reasoning_effort`` keys. Returns a value only when it is a known
    :data:`~command_center.models.EFFORT_LEVELS` level, else ``None`` — current Claude
    Code payloads carry none of these, so this is future-proofing that no-ops today.
    """
    from .models import EFFORT_LEVELS

    candidates = [data.get("effort"), data.get("effortLevel"), data.get("reasoningEffort")]
    model = data.get("model")
    if isinstance(model, dict):
        candidates.extend([model.get("effort"), model.get("reasoning_effort")])
    for value in candidates:
        if isinstance(value, str) and value in EFFORT_LEVELS:
            return value
    return None


def _capture_effort_from_payload(data: dict) -> None:
    """Defensively persist a session's ``--effort`` level from the status-line JSON.

    A HOT PATH (every statusline tick): pulls the session id + a reasoning-effort level
    from *data* and writes it ONLY when it differs from the stored value
    (read-compare-first). Covers a ``/effort`` switch made mid-session, which the launch
    flag captured by ``core.reconcile`` would otherwise miss. Silent on every error — the
    status line must never break. A payload with no effort key is a no-op (intended).
    """
    try:
        level = _effort_from_statusline_payload(data)
        if not level:
            return
        sid = data.get("session_id") or data.get("sessionId")
        if not isinstance(sid, str) or not sid:
            return
        with Store() as store:
            session = store.get(sid)
            if session is not None and (session.effort or "") != level:
                store.update_fields(sid, effort=level)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        pass


_SL_WIDTH = 100  # status-line soft width: only the `/aim (1)` anchor row is clipped to it
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _vlen(text: str) -> int:
    """Visible length of *text*, ignoring ANSI colour escapes."""
    return len(_ANSI_RE.sub("", text))


def _aim_label(index: int) -> str:
    """The ``/aim`` prefix, carrying the AIM's 1-based running index when known.

    ``index`` is the current AIM's position in the session's history (1 = the first
    AIM ever defined, 2 = the next, …). ``<= 0`` means the index is unknown, so the
    bare ``/aim`` is used rather than a misleading number.
    """
    return f"/aim ({index})" if index >= 1 else "/aim"


def _first_aim_anchor(
    session: Session, index: int, changed: bool, green: str, dim: str, reset: str
) -> list[str]:
    """The always-visible ``/aim (1):`` anchor row — the done-condition as originally typed.

    The status line otherwise renders only the CURRENT revision, so once the AIM is
    sharpened (``/aim (2)``, ``(3)``, …) the goal the user actually typed scrolls out of
    view. This pins it: revision (1) is shown on its own row above the current one, dimmed
    so the actionable current AIM stays prominent.

    Returns no row when it would only duplicate what is already on screen: ``index <= 1``
    (the current AIM *is* revision (1)) or a this-turn transition out of revision (1)
    (``changed`` with ``index == 2`` already prints ``/aim (1): <old> ====>``). Sessions
    whose AIM predates history tracking have no recorded revision (1) and get no row.
    """
    if index <= 1 or (changed and index == 2):
        return []
    first = (session.first_short_aim or session.first_aim or "").strip()
    if not first:
        return []
    prefix = f"{green}{_aim_label(1)}:{reset} "
    body_w = max(24, _SL_WIDTH - _vlen(prefix))
    if len(first) > body_w:
        first = first[: body_w - 1] + "…"
    return [f"{prefix}{dim}{first}{reset}"]


def _aim_statusline_lines(
    session: Session,
    checked: int,
    total: int,
    threshold: int,
    colors: tuple[str, str, str, str],
    index: int,
) -> list[str]:
    """The ``/aim`` status-line row(s): AIM + running index + progress bar + vague marker.

    ``index`` is the current AIM's running number in the session's AIM history, so the
    prefix reads ``/aim (N):``. When the AIM changed this turn (``aim_prev`` set) it renders
    the transition ``/aim (N-1): <old>  ====> /aim (N): <new>`` — always on ONE row, however
    long: the status line is precious vertical space, so a wide row is left to the terminal's
    own soft wrap instead of being split into an old-AIM row, wrapped new-AIM rows and a
    trailing bar row.

    Whenever the current revision is not the first, :func:`_first_aim_anchor` prepends an
    ``/aim (1):`` row, so the originally typed done-condition stays visible for the whole
    session no matter how often the AIM is sharpened.

    The AIM text rendered is :func:`~command_center.models.display_aim` — the latest
    short label when one exists, else the full AIM. ``store.set_aim`` clears the label on
    every change, so a stale label from a prior revision can never show here.
    """
    from .models import (
        aim_score_pct,
        display_aim,
        done_bar_parts,
        effective_progress,
        low_aim_score,
        progress_bar,
    )

    green, dim, reset, red = colors
    if not session.aim:
        return [f"{green}/aim:{reset} {dim}(set one with /aim){reset}"]
    label = _aim_label(index)
    low_score = low_aim_score(session.aim, session.aim_score, threshold)
    chip_color = red if low_score else dim
    chip = f"{chip_color}{aim_score_pct(session.aim, session.aim_score)}{reset}"
    new_aim = display_aim(session) or ""
    fraction = effective_progress(session.manual_progress, checked, total)
    if session.aim_met:
        # Impartial checker judged the AIM fulfilled → red DONE stamped inside the bar. The
        # bar's glyph runs are painted 231 (white) — the SAME palette entry the filled letters
        # use as background — so █ cells and letter cells match exactly.
        left, done_word, right, fills = done_bar_parts(fraction, 8)
        pct = f" {int(round(fraction * 100))}%" if fraction is not None else ""
        white = "\033[38;5;231m"
        prog = (
            f"  {white}{left}{reset}"
            f"{_paint_done_word(done_word, fills, 231, 231)}"
            f"{white}{right}{reset}{pct}"
        )
    elif fraction is not None:
        prog = f"  {progress_bar(fraction, 8)} {int(round(fraction * 100))}%"
    else:
        prog = ""
    tag = f"  {dim}⚠ vague — sharpen it{reset}" if low_score else ""
    changed = bool(session.aim_prev) and session.aim_prev != session.aim
    anchor = _first_aim_anchor(session, index, changed, green, dim, reset)
    if not changed:
        return [*anchor, f"{green}{label}:{reset} {chip} {new_aim}{prog}{tag}"]
    bold = "\033[1m"
    old_part = f"{green}{_aim_label(index - 1)}:{reset} {dim}{session.aim_prev}{reset}"
    arrow = f"{bold}====>{reset} {green}{label}:{reset} "
    # Always ONE line — old AIM, arrow, new AIM, bar and marker never split across rows;
    # an over-wide line soft-wraps in the terminal rather than costing three status-line rows.
    return [*anchor, f"{old_part}   {arrow}{chip} {new_aim}{prog}{tag}"]


def cmd_statusline(args: argparse.Namespace) -> int:
    """Emit the extra status-line rows: aim + progress, status + next step, and —
    when the session has a live TodoWrite list — a one-line ``done/total`` + boxes row.

    ANSI is always emitted (Claude Code renders it). Kept to a single fast SQLite
    lookup plus one registry scan so it is safe to call ~once per second.

    ``--print-glyph`` is a DISTINCT, standalone mode: it prints only the current
    shell's account badge (:func:`accounts.current_account_glyph`) and returns
    immediately, before any Store/adapter access — cheaper than the main path, and
    meant to be called separately/early (e.g. before building the statusline's badge
    segment) rather than combined with ``--capture-usage``.
    """
    if getattr(args, "print_glyph", False):
        from . import accounts

        print(accounts.current_account_glyph(), end="")
        return 0
    from .models import (
        derive_status,
        loads_todos,
    )

    sid = args.session
    # stdin carries the status-line JSON. Read it once when we need usage capture OR a
    # session id we were not given, then reuse the parsed payload for both.
    if getattr(args, "capture_usage", False) or not sid:
        payload = _read_statusline_stdin()
        if payload is not None:
            if getattr(args, "capture_usage", False):
                _capture_usage_from_payload(payload)
            if not sid:
                derived = payload.get("session_id") or payload.get("sessionId")
                if isinstance(derived, str) and derived:
                    sid = derived
    if not sid:
        return 0
    green, dim, reset = "\033[38;5;42m", "\033[38;5;244m", "\033[0m"
    status_color = {
        "working": "\033[38;5;40m",
        "waiting_input": "\033[38;5;196m",  # input required — red, to stand out
        "halted": "\033[38;5;196m",  # rate-limit halt — red, to stand out
        "waiting_codex": "\033[38;5;214m",  # Codex quota reset wait — amber
        "idle": "\033[38;5;75m",
        "snoozed": "\033[38;5;40m",
        "parked": "\033[38;5;244m",
        "done": "\033[38;5;35m",
        "failed": "\033[38;5;196m",
    }
    with Store() as store:
        session = store.get(sid)
        if session is None:
            print(f"{green}/aim:{reset} {dim}(set one with /aim){reset}")
            print(f"{dim}Status: untracked   /next-step: —{reset}")
            return 0
        checked, total = store.progress(sid)
        todos = loads_todos(session.todos)
        # Running index of the current AIM (1 = first ever defined). A set-but-unrecorded
        # AIM (predates history tracking) has no rows yet, so it is the first → 1.
        aim_index = store.count_aim_history(sid) or (1 if session.aim else 0)
        armed = store.armed_draft_summary()

    # Prefer the live status (fresher than the stored value) when discoverable.
    status_value = session.status
    try:
        adapter = _adapter()
        for live in adapter.discover():
            if live.session_id == sid:
                halted = live.alive and adapter.is_halted(live.cwd, live.session_id)
                status_value = derive_status(live, session, halted=halted).value
                break
    except OSError:
        pass

    threshold = config.load_config().aim_score_threshold
    aim_lines = _aim_statusline_lines(
        session, checked, total, threshold, (green, dim, reset, status_color["failed"]), aim_index
    )

    scolor = status_color.get(status_value, dim)
    nxt = session.next_step.splitlines()[0] if session.next_step else "—"
    for line in aim_lines:
        print(line)
    print(f"Status: {scolor}{status_value}{reset}   /next-step: {nxt}")

    # Impartial-checker drift warning (the blue dot, in every session's status line).
    from .models import drift_unresolved

    if drift_unresolved(session):
        blue = "\033[38;5;39m"
        why = session.drift_reason or "see ccc subgoal-history"
        print(f"{blue}● sub-goal drift ({session.drift_severity}){reset}: {why}  (ccc ack-drift)")

    # Live TodoWrite list as a single line: left counter + each item's box & short label.
    if todos:
        print(_render_todos_line(todos, green, dim, reset, status_color))

    # Armed parked prompt(s): one chip in EVERY session's statusline — the soonest fire
    # time, overdue-aware (red once both dispatchers should have fired; see park.py).
    # A prompt attached to THIS session gets its own explicit line first.
    if session.fire_at:
        from . import park

        own = park.format_fire(session.fire_at)
        own_color = status_color["failed"] if own.startswith("overdue") else "\033[38;5;214m"
        print(f"{own_color}⏳ parked prompt for THIS session {own}{reset}")
    if armed:
        from . import park

        earliest, count = armed
        fire_text = park.format_fire(earliest)
        more = f"  +{count - 1} more" if count > 1 else ""
        chip_color = status_color["failed"] if fire_text.startswith("overdue") else dim
        print(f"{chip_color}⏳ parked prompt {fire_text}{more}  (ccc jobs){reset}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from . import daemon, service

    if args.install:
        return service.install()
    if args.uninstall:
        return service.uninstall()
    if getattr(args, "status", False):
        return service.status()
    if args.loop:
        interval = args.interval or config.load_config().daemon_interval_sec
        daemon.run_loop(interval)
        return 0
    report = daemon.run_once(
        dry_run=args.dry_run,
        do_reap=not args.no_reap,
        do_summary=not args.no_summary,
        do_progress=not args.no_progress,
        do_alerts=not args.no_alerts,
    )
    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}reaped={len(report.reaped)} done={len(report.done)} "
        f"summarized={len(report.summarized)} progressed={len(report.progressed)} "
        f"alerted={len(report.alerted)} pruned={len(report.pruned)} scored={len(report.scored)} "
        f"short_aimed={len(report.short_aimed)} copilot={int(report.copilot_refreshed)} "
        f"claude_usage={int(report.claude_refreshed)} "
        f"resume={int(report.resume_spawned)} temps_swept={report.temps_swept}"
    )
    if args.verbose:
        for label, ids in (
            ("reap", report.reaped),
            ("done", report.done),
            ("summary", report.summarized),
            ("progress", report.progressed),
            ("alert", report.alerted),
            ("prune", report.pruned),
            ("score", report.scored),
            ("short-aim", report.short_aimed),
        ):
            for session_id in ids:
                print(f"  {label}: {session_id}")
    return 0


def cmd_autoprogress(args: argparse.Namespace) -> int:
    """Auto-derive + auto-check sub-goals for AIM sessions (manual/testing entry point).

    With ``--session`` it runs one session; otherwise it runs the same capped pass
    the daemon uses. ``--dry-run`` proposes/derives without writing checks.
    """
    from . import autoprogress

    adapter = _adapter()
    with Store() as store:
        from .core import reconcile

        reconcile(store, adapter)
        if args.session:
            transcript = adapter.transcript_path("", args.session)
            session = store.get(args.session)
            if session is not None:
                transcript = adapter.transcript_path(session.cwd, args.session)
            results = [
                autoprogress.run_for_session(
                    store,
                    args.session,
                    transcript,
                    model=config.load_config().llm_model,
                    dry_run=args.dry_run,
                )
            ]
        else:
            results = autoprogress.run_pass(store, adapter, dry_run=args.dry_run)

    prefix = "[dry-run] " if args.dry_run else ""
    changed = [r for r in results if r.changed() or r.note]
    if not changed:
        print(f"{prefix}no eligible sessions")
        return 0
    for res in results:
        if res.derived:
            print(f"{prefix}{res.session_id[:8]} derived {len(res.derived)} sub-goals:")
            for text in res.derived:
                print(f"    - {text}")
        if res.checked:
            print(f"{prefix}{res.session_id[:8]} checked off:")
            for text in res.checked:
                print(f"    [x] {text}")
        if args.verbose and res.note and not res.changed():
            print(f"{prefix}{res.session_id[:8]} skipped: {res.note}")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    try:
        from .views import tui
    except ImportError:
        print(
            "Textual is required for the TUI. Reinstall with:\n"
            "  uv tool install --force --editable <repo>   (or: pip install textual)",
            file=sys.stderr,
        )
        return 1
    from . import terminal

    cfg = config.load_config()
    terminal.set_tab(cfg.tab_title or None, terminal.color_rgb(cfg.tab_color))
    try:
        return tui.run()
    finally:
        terminal.reset_tab_color()


def cmd_demo(args: argparse.Namespace) -> int:
    """Seed a throwaway demo store and open the TUI against it (never the real state)."""
    from . import demo

    return demo.run(args)


def cmd_tag(args: argparse.Namespace) -> int:
    """Manage the typed @tag registry (people=yellow, place=blue, status=green, …)."""
    from . import tags

    action = getattr(args, "tagcmd", None) or "list"
    if action == "add":
        try:
            tags.add_tag(args.name, args.type)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"added @{args.name.lstrip('@')} → {args.type}")
        return 0
    if action == "type":
        tags.add_type(args.name, args.color)
        print(f"type {args.name} = {args.color}")
        return 0
    # list
    type_map = tags.types()
    print("types:")
    for name, style in sorted(type_map.items()):
        print(f"  {name}: {style}")
    print("tags:")
    for token in tags.known_tags():
        style = tags.tag_style(token[1:]) or "?"
        print(f"  {token}  ({style})")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Serve the same TUI in the browser via textual-serve (localhost only)."""
    import shutil

    try:
        from textual_serve.server import Server
    except ImportError:
        print(
            "textual-serve is required. Reinstall with:\n"
            "  uv tool install --force --editable <repo>   (or: pip install textual-serve)",
            file=sys.stderr,
        )
        return 1
    ccc = shutil.which("ccc") or "ccc"
    url = f"http://{args.host}:{args.port}"
    print(f"Serving the command center at {url}  (Ctrl-C to stop)")
    Server(f"{ccc} tui", host=args.host, port=args.port).serve()
    return 0


def cmd_peek(args: argparse.Namespace) -> int:
    """Show my last prompt for the iTerm-focused session in a floating panel."""
    from . import peek

    return peek.run(args)


def cmd_jump(args: argparse.Namespace) -> int:
    """Focus the iTerm tab running the ccc TUI (bound to a Karabiner chord)."""
    from . import jump

    return jump.run(args)


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Save the whole iTerm layout (windows/tabs/splits + what each pane runs)."""
    from . import snapshot

    return snapshot.run_snapshot(args)


def cmd_restore_snapshot(args: argparse.Namespace) -> int:
    """Rebuild a saved iTerm layout: resume the Claude sessions, re-run the rest."""
    from . import snapshot

    return snapshot.run_restore(args)


_RESTART_POLL_SEC = 0.1
_RESTART_TIMEOUT_SEC = 5.0


def cmd_restart_tui(args: argparse.Namespace) -> int:
    """Restart the running ccc TUI in its own terminal tab (for automations).

    An automation that just changed ccc's code/config (editable install) runs this to
    bounce the live TUI so the new code is loaded — same tab, no manual keystroke. It
    asks the TUI (via the ``restart`` jumpstate verb) to exit and re-exec itself in place.

    Exit 0 once the TUI has come back up (the request was consumed and a live TUI
    identity is re-registered — a same-pid ``execv`` restart after a teardown gap, or a
    fresh pid); exit 1 when no TUI is running, or the restart did not complete in 5 s.
    """
    import time

    from . import jumpstate

    tui = jumpstate.get_tui()
    if tui is None:
        print("no running ccc TUI", file=sys.stderr)
        return 1
    old_pid = tui[0]
    jumpstate.request_restart()
    saw_consumed = False  # the TUI acknowledged (cleared) the request
    saw_gone = False  # its identity was cleared on unmount → restart is genuinely in flight
    deadline = time.monotonic() + _RESTART_TIMEOUT_SEC
    while time.monotonic() < deadline:
        time.sleep(_RESTART_POLL_SEC)
        if not saw_consumed and not jumpstate.peek_restart():
            saw_consumed = True
        current = jumpstate.get_tui()
        if current is None:
            if saw_consumed:
                saw_gone = True
            continue
        new_pid = current[0]
        if new_pid != old_pid:  # a fresh process registered — unambiguous restart
            print(f"restarted ccc TUI (pid {old_pid} → {new_pid})")
            return 0
        if saw_consumed and saw_gone:  # same pid re-registered after the gap (execv in place)
            print(f"restarted ccc TUI (pid {old_pid} → {new_pid})")
            return 0
    jumpstate.clear_restart()  # stale-request safety: don't let it restart a TUI started later
    print(
        "error: ccc TUI did not restart within 5s — is it wedged? "
        "check the tab (or start it with `ccc tui`)",
        file=sys.stderr,
    )
    return 1


def cmd_tab_symbol(args: argparse.Namespace) -> int:
    """Claim/read this iTerm tab's badge, or print a path's deterministic repo symbol.

    Three modes:

    * ``--print [PATH]`` — print the DETERMINISTIC per-repo symbol for PATH (or the
      cwd). No iTerm dependency, so this is what the cross-terminal shell badge hook
      (``ccc install-shell``) consumes on Linux and any plain terminal. ``--color``
      also prints the folder's colour (hex/name).
    * ``--sync`` — re-apply every tracked live session's ``<emoji> repo`` title to its
      iTerm tab via AppleScript (badge tabs already open before the hook landed).
    * default — claim (or ``--read``) this iTerm tab's unique badge keyed by
      ``$ITERM_SESSION_ID`` and print it (the macOS zsh ``chpwd`` hook path).

    With nothing to key on it prints nothing and exits 0.
    """
    from . import tabsymbol

    if getattr(args, "print_only", False):
        path = args.path or os.getcwd()
        repo_symbol = tabsymbol.symbol_for_repo(path)
        if repo_symbol:
            if getattr(args, "color", False):
                from . import colors

                print(f"{repo_symbol} {colors.folder_style(os.path.expanduser(path))}")
            else:
                print(repo_symbol)
        return 0

    if args.sync:
        with Store() as store:
            badged = tabsymbol.sync_live(store)
        print(f"synced {len(badged)} tab title(s)")
        return 0

    iterm = args.session_id or os.environ.get("ITERM_SESSION_ID")
    if args.read:
        symbol = tabsymbol.read(iterm)
    else:
        symbol = tabsymbol.assign(iterm, folder=os.getcwd())
    if symbol:
        print(symbol)
    return 0


# --------------------------------------------------------------------------- #
# argument parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccc",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Command center for parked Claude Code sessions (AIM, progress, next-step).",
        epilog=(
            "background daemon: a launchd agent runs `ccc daemon` every few minutes to\n"
            "auto-close idle sessions, refresh summaries and fire alerts — see\n"
            "`ccc daemon --help` for what it does and how to install / uninstall it."
            # argparse only ever PRINTS an epilog for --help, but build_parser runs on
            # every invocation (`ccc statusline` fires on each prompt render). Resolving
            # the routing block costs a config read plus an `ai routing` subprocess, so it
            # is built only when this run is actually asking for help.
            + ("\n\n" + llmrouting.render() if llmrouting.help_requested() else "")
        ),
    )
    parser.add_argument("--version", action="version", version=f"ccc {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("ls", help="flat clickable list of all tracked sessions").set_defaults(
        func=cmd_ls
    )

    sub.add_parser(
        "llm-routing", help="which model each ccc LLM action uses now (and what it bills)"
    ).set_defaults(func=cmd_llm_routing)

    p_aim = sub.add_parser("aim", help="status-line lookup of a session's done-condition")
    p_aim.add_argument("--session")
    p_aim.add_argument("--format", choices=["statusline", "plain", "bar"], default="statusline")
    p_aim.set_defaults(func=cmd_aim)

    p_sl = sub.add_parser("statusline", help="emit the two extra status-line rows for a session")
    p_sl.add_argument(
        "--session",
        help="session id; omit to derive it from the session_id in the status-line JSON on stdin",
    )
    p_sl.add_argument(
        "--capture-usage",
        action="store_true",
        help="also persist the account /usage snapshot from the status-line JSON on stdin",
    )
    p_sl.add_argument(
        "--print-glyph",
        action="store_true",
        help="print only the current account's badge glyph (🏠/💼) and exit",
    )
    p_sl.set_defaults(func=cmd_statusline)

    p_set_aim = sub.add_parser("set-aim", help="set 'this session is done when: ...'")
    p_set_aim.add_argument("text")
    p_set_aim.add_argument("--session")
    p_set_aim.add_argument(
        "-f",
        "--first",
        action="store_true",
        help="rewrite the FIRST recorded AIM (/aim (1)) in place instead of setting "
        "the current one — fixes how the original goal was stated, adds no revision",
    )
    p_set_aim.set_defaults(func=cmd_set_aim)

    p_score = sub.add_parser("score-aim", help="internal: refine the AIM specificity score (LLM)")
    p_score.add_argument("--session")
    p_score.add_argument(
        "--dry-run",
        metavar="AIM",
        help="score this candidate AIM with the independent rubric checker (prints JSON, no save)",
    )
    p_score.set_defaults(func=cmd_score_aim)

    p_short = sub.add_parser(
        "short-aim", help="internal: generate the short-AIM column label (cheap codex run)"
    )
    p_short.add_argument("--session")
    p_short.add_argument(
        "--dry-run",
        metavar="AIM",
        help="print the short label this candidate AIM would produce (no save)",
    )
    p_short.set_defaults(func=cmd_short_aim)

    p_cop = sub.add_parser(
        "copilot-usage", help="refresh+print this month's GitHub Copilot usage (gh billing API)"
    )
    p_cop.add_argument(
        "-j", "--json", action="store_true", help="dump the cached snapshot as JSON (no fetch)"
    )
    p_cop.set_defaults(func=cmd_copilot_usage)

    sub.add_parser(
        "restart-tui",
        help="internal: restart the running ccc TUI in its own tab (for automations)",
    ).set_defaults(func=cmd_restart_tui)

    p_clu = sub.add_parser(
        "claude-usage",
        help="internal: fetch+cache each account's Claude /usage (OAuth endpoint)",
    )
    p_clu.add_argument(
        "-a", "--account", metavar="LABEL", help="fetch just this account (default: all)"
    )
    p_clu.set_defaults(func=cmd_claude_usage)

    p_cdu = sub.add_parser(
        "codex-usage",
        help=(
            "refresh+print the live OpenAI Codex usage (chatgpt.com wham/usage) "
            "for each configured CODEX_HOME"
        ),
    )
    p_cdu.add_argument(
        "-j", "--json", action="store_true", help="dump the cached snapshots as JSON (no fetch)"
    )
    p_cdu.add_argument(
        "-a",
        "--account",
        metavar="LABEL",
        help="fetch just this CODEX_HOME (default | private; default: all)",
    )
    p_cdu.set_defaults(func=cmd_codex_usage)

    p_drift = sub.add_parser("check-drift", help="internal: impartial sub-goal drift check (LLM)")
    p_drift.add_argument("--session")
    p_drift.set_defaults(func=cmd_check_drift)

    p_assess = sub.add_parser(
        "assess-aim", help="internal: impartial 'is the AIM fulfilled?' self-assessment (LLM)"
    )
    p_assess.add_argument("--session")
    p_assess.set_defaults(func=cmd_assess_aim)

    p_ackd = sub.add_parser(
        "ack-drift", help="acknowledge a flagged sub-goal drift (clears the dot)"
    )
    p_ackd.add_argument("--session")
    p_ackd.set_defaults(func=cmd_ack_drift)

    p_aimhist = sub.add_parser(
        "aim-history", help="show the session's AIM progression (first→current)"
    )
    p_aimhist.add_argument("--session")
    p_aimhist.set_defaults(func=cmd_aim_history)

    p_handoff = sub.add_parser(
        "handoff", help="commit+push a locked file, then release it for a waiting session"
    )
    p_handoff.add_argument("file", help="the file to commit, push and unlock")
    p_handoff.add_argument("-m", "--message", default="", help="commit message (auto if omitted)")
    p_handoff.add_argument("--session")
    p_handoff.set_defaults(func=cmd_handoff)

    p_lockrel = sub.add_parser(
        "lock-release", help="force-release a file lock without committing (prefer `handoff`)"
    )
    p_lockrel.add_argument("file", nargs="?", default="", help="file to unlock (omit for --all)")
    p_lockrel.add_argument(
        "--all", action="store_true", help="release every lock this session holds"
    )
    p_lockrel.add_argument("--session")
    p_lockrel.set_defaults(func=cmd_lock_release)

    p_locks = sub.add_parser("locks", help="list active cross-session file locks")
    p_locks.set_defaults(func=cmd_locks)

    p_sghist = sub.add_parser(
        "subgoal-history", help="show the sub-goal checklist's evolution + drift verdicts"
    )
    p_sghist.add_argument("--session")
    p_sghist.set_defaults(func=cmd_subgoal_history)

    p_next = sub.add_parser("set-next", help="set/override the next step (marks it user-authored)")
    p_next.add_argument("text")
    p_next.add_argument("--session")
    p_next.set_defaults(func=cmd_set_next)

    p_block = sub.add_parser("set-blocked", help="set what the session is blocked/waiting on")
    p_block.add_argument("text", nargs="?", default="")
    p_block.add_argument("--session")
    p_block.set_defaults(func=cmd_set_blocked)

    p_dead = sub.add_parser("set-deadline", help="set an ISO-8601 finish-by date (blank to clear)")
    p_dead.add_argument("date", nargs="?", default="")
    p_dead.add_argument("--session")
    p_dead.set_defaults(func=cmd_set_deadline)

    p_dc = sub.add_parser("set-donecheck", help="set a shell command whose exit 0 means done")
    p_dc.add_argument("command", nargs="?", default="")
    p_dc.add_argument("--session")
    p_dc.set_defaults(func=cmd_set_donecheck)

    p_sg = sub.add_parser("subgoals", help="set (or --list) the progress checklist")
    p_sg.add_argument("items", nargs="*")
    p_sg.add_argument("--list", action="store_true")
    p_sg.add_argument("--session")
    p_sg.add_argument(
        "--adaptive", action="store_true", help="re-derive this checklist when the AIM changes"
    )
    p_sg.add_argument(
        "--merge", action="store_true", help="carry over ticks for items whose text is unchanged"
    )
    p_sg.add_argument("--source", choices=("user", "agent", "auto"), default="user")
    p_sg.set_defaults(func=cmd_subgoals)

    p_check = sub.add_parser("check", help="check/uncheck a checklist item by position (1-based)")
    p_check.add_argument("position", type=int, help="1-based position in the session's checklist")
    p_check.add_argument("--uncheck", action="store_true")
    p_check.add_argument("--session")
    p_check.set_defaults(func=cmd_check)

    p_sgc = sub.add_parser(
        "subgoal-check",
        help="attach a shell predicate to a sub-goal by position (exit 0 ticks it; empty clears)",
    )
    p_sgc.add_argument("position", type=int, help="1-based position in the session's checklist")
    p_sgc.add_argument("command", nargs="?", default="")
    p_sgc.add_argument("--session")
    p_sgc.set_defaults(func=cmd_subgoal_check)

    p_todos = sub.add_parser("todos", help="show a session's live TodoWrite/Task list")
    p_todos.add_argument("--session")
    p_todos.set_defaults(func=cmd_todos)

    p_done = sub.add_parser("mark-done", help="mark a session done (--undo to reopen)")
    p_done.add_argument("--session")
    p_done.add_argument("-q", "--quiet", action="store_true", help="suppress the summary print")
    # --close arms a close-after-turn; --undo reopens. The two are mutually exclusive (an
    # argparse error rejects `--close --undo` together, e.g. `ccc mark-done -c -q`).
    p_done_ex = p_done.add_mutually_exclusive_group()
    p_done_ex.add_argument(
        "-c",
        "--close",
        action="store_true",
        help="also close the session's pane/tab after the turn",
    )
    p_done_ex.add_argument("-u", "--undo", action="store_true", help="reopen a session marked done")
    p_done.set_defaults(func=cmd_mark_done)

    p_closenow = sub.add_parser(
        "close-now",
        help="internal: SIGTERM + close the session's tab after a mark-done --close (spawned)",
    )
    p_closenow.add_argument("-s", "--session", help="the session id to close")
    p_closenow.add_argument(
        "-i", "--iterm", default="", help="the fresh $ITERM_SESSION_ID to close (hook-supplied)"
    )
    p_closenow.set_defaults(func=cmd_close_now)

    p_keep = sub.add_parser("keep", help="exempt a session from the idle reaper (--off to clear)")
    p_keep.add_argument("--session")
    p_keep.add_argument("--off", action="store_true")
    p_keep.set_defaults(func=cmd_keep)

    p_ti = sub.add_parser(
        "toggle-idle",
        help="mute/unmute Claude Code's idle 'waiting for input' macOS popups (TUI `ti`)",
        description=(
            "Flip agentPushNotifEnabled in Claude Code's settings.json — the source of the "
            "'a session is waiting for your input' desktop popups. No flag toggles; -n/--on and "
            "-f/--off force the state. Global (every session; also covers permission prompts); a "
            "running session may need a restart to apply."
        ),
    )
    p_ti_grp = p_ti.add_mutually_exclusive_group()
    p_ti_grp.add_argument("-n", "--on", action="store_true", help="force popups ON")
    p_ti_grp.add_argument("-f", "--off", action="store_true", help="force popups OFF")
    p_ti.set_defaults(func=cmd_toggle_idle)

    p_resume = sub.add_parser("resume", help="resume a session in this terminal")
    p_resume.add_argument("session_id")
    p_resume.set_defaults(func=cmd_resume)

    p_park = sub.add_parser(
        "park",
        help="park a ready prompt in THIS tab and auto-launch it when the rate limit resets",
    )
    p_park.add_argument(
        "prompt", nargs="?", default=None, help="the prompt (or use -c / piped stdin / $EDITOR)"
    )
    p_park.add_argument(
        "-c",
        "--clipboard",
        action="store_true",
        help="take the prompt from the clipboard (pbpaste)",
    )
    p_park.add_argument(
        "-N", "--now", action="store_true", help="skip the wait — launch immediately"
    )
    p_park.add_argument(
        "-n",
        "--no-auto",
        action="store_true",
        help="count down, then ring and wait for Enter instead of auto-launching",
    )
    p_park.add_argument(
        "-w",
        "--window",
        choices=("five_hour", "seven_day", "fable_week"),  # keep in sync: park.WINDOW_CHOICES
        default="five_hour",
        help="rate-limit window whose reset fires the launch (default: five_hour)",
    )
    p_park.add_argument(
        "-b",
        "--buffer",
        type=int,
        default=90,
        help="seconds past the reset before firing (default: 90)",
    )
    p_park.add_argument(
        "-a", "--aim", default=None, help="AIM for the job (default: first line of the prompt)"
    )
    p_park.add_argument(
        "-e",
        "--editor",
        action="store_true",
        help="capture the prompt in $EDITOR instead of the floating park panel",
    )
    p_park.add_argument(
        "-g",
        "--grab",
        action="store_true",
        help="global-chord mode (Karabiner q+p): park panel over the frontmost iTerm "
        "tab — over a live Claude session the prompt attaches to THAT session "
        "(delivered into it at the reset); otherwise an armed job fires in a NEW "
        "tab via the daemon",
    )
    p_park.add_argument(
        "-j",
        "--new-job",
        action="store_true",
        help="with -g: always register a detached NEW-session job, even when the "
        "frontmost tab is a live Claude session (skip attach)",
    )
    p_park.set_defaults(func=cmd_park)

    p_newjob = sub.add_parser(
        "new-job", help="register a future job (saved AIM + prompt) to start later"
    )
    p_newjob.add_argument("-a", "--aim", required=True, help="the done-condition (required)")
    p_newjob.add_argument(
        "-p", "--prompt", default=None, help="prompt to run when started (defaults to the AIM)"
    )
    p_newjob.add_argument(
        "-c", "--cwd", default=None, help="repo working directory (default: current dir)"
    )
    p_newjob.add_argument("-D", "--deadline", default=None, help="finish-by date (YYYY-MM-DD)")
    p_newjob.add_argument(
        "-w", "--when", default=None, help="when you intend to start (e.g. 'during holidays')"
    )
    p_newjob.add_argument(
        "-d",
        "--depends-on",
        default=None,
        help="another job (full UUID or unique id/hash prefix) this one must wait for; "
        "the launch guards warn until that job is done",
    )
    p_newjob.add_argument(
        "-s",
        "--start-date",
        default=None,
        help="FIXED start date YYYY-MM-DD — sinks the job into the SCHEDULED section "
        "(very bottom) and makes launching before that date ask for confirmation",
    )
    p_newjob.add_argument(
        "-j",
        "--job-type",
        choices=JOB_TYPES,
        default="claude",
        help="launch type: claude (normal), codex (delegate patch), codex-write (codex edits)",
    )
    p_newjob.add_argument(
        "-O",
        "--overseer",
        choices=LLM_CHOICES,
        default=DEFAULT_LLM,
        help=f"model the session runs on (default: {DEFAULT_LLM})",
    )
    p_newjob.add_argument(
        "-E",
        "--executor",
        choices=LLM_CHOICES,
        default=DEFAULT_LLM,
        help=f"model subagents implement on when it differs from overseer (default: {DEFAULT_LLM})",
    )
    p_newjob.add_argument(
        "-A",
        "--account",
        default=None,
        help="Claude account label to launch (bill) under (default: the default account)",
    )
    p_newjob.add_argument(
        "-N",
        "--no-codex",
        action="store_true",
        help="ban every Codex integration in the launched session (CCC_NO_CODEX=1); "
        "refused together with -j codex / -j codex-write",
    )
    p_newjob.add_argument(
        "-K",
        "--idempotency-key",
        default=None,
        metavar="KEY",
        help="create-or-retrieve key: re-running with the same KEY returns the SAME job "
        "instead of registering a second one (a KEY reused for different cwd/aim/"
        "--no-codex exits 2)",
    )
    p_newjob.add_argument(
        # -j is --job-type here, so the JSON switch takes the capital (short forms stay unique).
        "-J",
        "--json",
        action="store_true",
        help="print one machine-readable JSON line (session_id/created/account/no_codex) "
        "and nothing else",
    )
    p_newjob.add_argument(
        "-R",
        "--at-reset",
        action="store_true",
        help="arm the job to auto-fire (daemon, new tab) when the rate-limit window resets",
    )
    p_newjob.add_argument(
        "-W",
        "--fire-window",
        choices=("five_hour", "seven_day", "fable_week"),  # keep in sync: park.WINDOW_CHOICES
        default="five_hour",
        help="which window's reset fires an --at-reset job (default: five_hour)",
    )
    p_newjob.set_defaults(func=cmd_new_job)

    p_newprompt = sub.add_parser(
        "new-prompt",
        help="create a fresh FUTURE-job capture file (prefilled draft) and print its path",
    )
    p_newprompt.add_argument(
        "-r", "--repo", default=None, help="place it under <cat>/<repo> (default: the future root)"
    )
    p_newprompt.add_argument(
        "-o", "--open", action="store_true", help="also open the file in Obsidian (obsidian:// URI)"
    )
    p_newprompt.set_defaults(func=cmd_new_prompt)

    p_startjob = sub.add_parser(
        "start-job", help="launch a saved future job (exec claude --session-id in its repo)"
    )
    p_startjob.add_argument("session_id")
    p_startjob.add_argument(
        "-F",
        "--force",
        action="store_true",
        help="skip the premature-start confirmation (fixed start date not yet reached)",
    )
    p_startjob.add_argument(
        "-u",
        "--auto",
        action="store_true",
        help="internal: unattended dispatch (daemon/park) — a tripped guard disarms "
        "the job's auto-fire instead of asking",
    )
    p_startjob.set_defaults(func=cmd_start_job)

    p_fireattached = sub.add_parser(
        "fire-attached",
        help="internal: resume a session with its parked (attached) prompt",
    )
    p_fireattached.add_argument("session_id")
    p_fireattached.set_defaults(func=cmd_fire_attached)

    p_claimfire = sub.add_parser(
        "claim-fire",
        help="internal: one-shot claim of a session's armed parked prompt (prints it)",
    )
    p_claimfire.add_argument("session_id")
    p_claimfire.set_defaults(func=cmd_claim_fire)

    p_openjob = sub.add_parser(
        "open-job",
        help="open a future job in a new iTerm tab (like the TUI's r; safe from Obsidian)",
    )
    p_openjob.add_argument("session_id", nargs="?", help="future-job id (short hash or full UUID)")
    p_openjob.add_argument(
        "-f",
        "--file",
        help="read the job's session_id from this markdown file instead of a positional id "
        "(used by the in-note Obsidian '▶ Start this job' button)",
    )
    p_openjob.set_defaults(func=cmd_open_job)

    p_donejob = sub.add_parser(
        "done-job",
        help="mark a future job done without running it (moves to the DONE list/mirror)",
    )
    p_donejob.add_argument("session_id", nargs="?", help="future-job UUID")
    p_donejob.add_argument(
        "-f",
        "--file",
        help="read the job's session_id from this markdown file "
        "(used by the in-note '✓ Mark job as done' button)",
    )
    p_donejob.set_defaults(func=cmd_done_job)

    p_deljob = sub.add_parser(
        "delete-job",
        help="move a future job to the vault delete/ trash (restorable with restore-job)",
    )
    p_deljob.add_argument("session_id", nargs="?", help="future-job UUID")
    p_deljob.add_argument(
        "-f",
        "--file",
        help="read the job's session_id from this markdown file "
        "(used by the in-note '🗑 Delete job' button)",
    )
    p_deljob.set_defaults(func=cmd_delete_job)

    p_restjob = sub.add_parser(
        "restore-job",
        help="stage a deleted job back into FUTURE (inverse of delete-job)",
    )
    p_restjob.add_argument("session_id", nargs="?", help="future-job UUID")
    p_restjob.add_argument(
        "-f",
        "--file",
        help="read the job's session_id from this markdown file "
        "(used by the in-note '↩ Stage job back in' button and the delete dashboard)",
    )
    p_restjob.set_defaults(func=cmd_restore_job)

    p_archive = sub.add_parser(
        "archive",
        help="soft-hide a parked session (tp lists it instead); stays resumable with "
        "`ccc resume` — -u undoes",
    )
    p_archive.add_argument("session_id", help="session UUID or unique prefix")
    p_archive.add_argument(
        "-u", "--undo", action="store_true", help="clear the archived flag again"
    )
    p_archive.set_defaults(func=cmd_archive)

    p_focusjob = sub.add_parser(
        "focus-job",
        help="bring a LIVE session's iTerm tab to the front (verifies it's live first)",
    )
    p_focusjob.add_argument("session_id")
    p_focusjob.set_defaults(func=cmd_focus_job)

    p_resumejob = sub.add_parser(
        "resume-job",
        help="resume a parked session in a new iTerm tab (focuses the tab if it is live)",
    )
    p_resumejob.add_argument("session_id")
    p_resumejob.set_defaults(func=cmd_resume_job)

    p_unlaunch = sub.add_parser(
        "unlaunch",
        help="return a launched job to FUTURE (draft) — requires its tab be closed first",
    )
    p_unlaunch.add_argument("session_id")
    p_unlaunch.set_defaults(func=cmd_unlaunch)

    p_jobs = sub.add_parser("jobs", help="list registered future jobs (drafts)")
    p_jobs.add_argument(
        "-j", "--json", action="store_true", help="print the job list as one JSON document"
    )
    p_jobs.set_defaults(func=cmd_jobs)

    p_jobacct = sub.add_parser(
        "job-account",
        help="Show per-account usage urgency and which account new jobs will bill to",
    )
    p_jobacct.add_argument(
        "-p",
        "--pick",
        action="store_true",
        help="print only the picked account label (for shell wrappers)",
    )
    p_jobacct.set_defaults(func=cmd_job_account)

    p_quota = sub.add_parser(
        "quota",
        help="fast cache-first quota oracle: which provider/account still has tokens",
    )
    p_quota.add_argument(
        "-j", "--json", action="store_true", help="emit the versioned JSON contract"
    )
    p_quota.add_argument(
        "-p",
        "--provider",
        metavar="ID",
        help="report ONE provider; exit 0=available 1=blocked 2=unknown/disabled",
    )
    p_quota.add_argument(
        "-b",
        "--best",
        action="store_true",
        help="print the best CLAUDE account label only (empty if none usable)",
    )
    p_quota.add_argument(
        "-M",
        "--model",
        default="",
        metavar="NAME",
        help="scope the Claude windows to the model you intend to call",
    )
    p_quota.add_argument(
        "-r",
        "--refresh",
        action="store_true",
        help="re-fetch live usage first (the ONLY networked path)",
    )
    p_quota.add_argument(
        "-m", "--mark", metavar="ID", help="record an authoritative block for a provider"
    )
    p_quota.add_argument(
        "-u",
        "--retry-after",
        type=int,
        metavar="SEC",
        help="seconds until the marked provider unblocks (with -m)",
    )
    p_quota.add_argument(
        "-U",
        "--until",
        metavar="STAMP",
        help="local ISO deadline for -m, EXCLUSIVE (blocked while now < STAMP), "
        "e.g. 2026-09-07T00:00",
    )
    p_quota.add_argument(
        "-H",
        "--hold",
        action="store_true",
        help="mark as an administrative HOLD: observed rejections/successes cannot "
        "overwrite or clear it — only expiry or an explicit -c",
    )
    p_quota.add_argument("-R", "--reason", default="", help="why the provider is blocked (with -m)")
    p_quota.add_argument(
        "-s",
        "--scope",
        default="",
        help="block scope tag for -m (e.g. 'auth' = entitlement/login failure; "
        "consumers may treat auth-scoped blocks as not worth a last-resort retry)",
    )
    p_quota.add_argument("-c", "--clear", metavar="ID", help="drop a provider's block")
    p_quota.add_argument(
        "-O",
        "--observed-only",
        action="store_true",
        help="with -c: clear only an observed block, never a hold (the success-path mode)",
    )
    p_quota.set_defaults(func=cmd_quota)

    p_rm = sub.add_parser("rm", help="remove a tracked session from the command center")
    p_rm.add_argument("session_id")
    p_rm.set_defaults(func=cmd_rm)

    p_prune = sub.add_parser(
        "prune",
        help=(
            "delete leftover rows: contentless junk, headless `claude -p` one-shots, and "
            "dead-launched jobs (started but never had a turn → no resumable transcript)"
        ),
    )
    p_prune.add_argument("--dry-run", action="store_true", help="list candidates without deleting")
    p_prune.set_defaults(func=cmd_prune)

    p_syncf = sub.add_parser(
        "sync-future",
        help="internal: reconcile FUTURE-job drafts with their Obsidian files (flock singleton)",
    )
    p_syncf.add_argument(
        "-f", "--file", default=None, help="targeted single-file import (e.g. before start-job)"
    )
    p_syncf.add_argument(
        "-v", "--verbose", action="store_true", help="print the per-item detail log"
    )
    p_syncf.set_defaults(func=cmd_sync_future)

    p_syncm = sub.add_parser(
        "sync-mirrors",
        help="internal: export RUNNING/DONE session mirrors to the vault (flock singleton)",
    )
    p_syncm.add_argument(
        "-v", "--verbose", action="store_true", help="print the per-item detail log"
    )
    p_syncm.set_defaults(func=cmd_sync_mirrors)

    p_hook = sub.add_parser("hook", help="internal: invoked by Claude Code hooks")
    p_hook.add_argument(
        "event",
        choices=[
            "session-start",
            "user-prompt",
            "pre-tool-use",
            "post-tool-use",
            "stop",
            "release-locks",
            "session-end",
            "pre-compact",
            "subagent-stop",
        ],
    )
    p_hook.set_defaults(func=cmd_hook)

    p_ih = sub.add_parser(
        "install-hooks",
        help="merge ccc's hook wiring into $CLAUDE_HOME/settings.json (idempotent)",
        description=(
            "Install (or update) the Claude Code hook entries ccc owns — SessionStart, "
            "UserPromptSubmit, Pre/PostToolUse, Stop (+ release-locks last), SessionEnd, "
            "PreCompact, SubagentStop — as `<ccc> hook <event>` commands. Idempotent: a "
            "rerun replaces ccc's own entries in place and never touches foreign hooks. "
            "settings.json is backed up (settings.json.ccc-backup-<UTCts>) before writing, "
            "and written symlink-safely (through a stow symlink to its real target)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_ih.add_argument(
        "-n", "--dry-run", action="store_true", help="print a unified diff; write nothing"
    )
    p_ih.add_argument(
        "-u", "--uninstall", action="store_true", help="remove only ccc-owned hook entries"
    )
    p_ih.set_defaults(func=cmd_install_hooks)

    p_is = sub.add_parser(
        "install-statusline",
        help="wire ccc's status line into settings.json (chain an existing one with -c)",
        description=(
            "Set ccc's statusLine command. With no existing statusLine it installs "
            "`<ccc> statusline --capture-usage` directly. If a foreign statusLine is "
            "already configured it REFUSES unless -c/--chain is given, which generates "
            "$CLAUDE_HOME/command-center/statusline-chain.sh (runs the original first "
            "under a 2s timeout, then appends ccc's rows) and points statusLine at it. "
            "Backed up + written symlink-safely; --uninstall restores the recorded original."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_is.add_argument(
        "-c",
        "--chain",
        action="store_true",
        help="chain an existing foreign statusLine instead of refusing to overwrite it",
    )
    p_is.add_argument(
        "-n", "--dry-run", action="store_true", help="print a unified diff; write nothing"
    )
    p_is.add_argument(
        "-u",
        "--uninstall",
        action="store_true",
        help="remove ccc's statusLine (restoring the chained original when present)",
    )
    p_is.set_defaults(func=cmd_install_statusline)

    p_doc = sub.add_parser(
        "doctor",
        help="read-only health check of the ccc install + environment (exit 1 on any ❌)",
        description=(
            "Print a sectioned ✅/❌/− report: the claude CLI, config.toml, which hooks + "
            "statusline are wired, the daemon launchd agent (macOS), and per-feature "
            "dependency checks driven by the config flags (gh, codex, session-continue, "
            "vault root, launcher). Mutates nothing; exits 0 when healthy, 1 on any ❌."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_doc.set_defaults(func=cmd_doctor)

    p_ic = sub.add_parser(
        "install-commands",
        help="copy ccc's slash commands into $CLAUDE_HOME/commands (-x also the codex pair)",
        description=(
            "Install the seven ccc slash commands (aim, next-step, done, block, deadline, "
            "aim-history, subgoal-history) into $CLAUDE_HOME/commands. With -x/--codex also "
            "installs the optional Codex delegate command + skill (into commands/ and "
            "skills/). Idempotent: a byte-identical file is skipped, an existing one is "
            "backed up before overwrite. -u/--uninstall removes only files whose content "
            "still matches what ccc installed (a user-edited command is left alone)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_ic.add_argument(
        "-x", "--codex", action="store_true", help="also install the codex command + skill"
    )
    p_ic.add_argument(
        "-n", "--dry-run", action="store_true", help="show what would change; write nothing"
    )
    p_ic.add_argument(
        "-u", "--uninstall", action="store_true", help="remove only unmodified ccc-installed files"
    )
    p_ic.set_defaults(func=cmd_install_commands)

    p_ish = sub.add_parser(
        "install-shell",
        help="opt-in shell rc block: AIM-at-startup wrapper + cross-terminal tab badges",
        description=(
            "Write a markered block into your shell rc (~/.zshrc for zsh, ~/.bashrc for bash, "
            "detected from $SHELL) with two independent pieces: an AIM-at-startup wrapper "
            "(a shell function, default name 'c', that asks this session's done-condition and "
            "launches claude with CLAUDE_SESSION_AIM set) and cross-terminal tab badges (a "
            "precmd/chpwd hook that titles the tab '<symbol> <repo-leaf>' via plain OSC, so it "
            "works on gnome-terminal / any emulator, not just iTerm). Idempotent (rerun "
            "replaces the block), timestamped backup, -u/--uninstall removes only the block, "
            "-n/--dry-run prints it. If a command named after the wrapper already exists it "
            "refuses (pass -w/--wrapper-name or --no-wrapper)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_ish.add_argument("-r", "--rc", default=None, help="rc file to edit (default: per $SHELL)")
    p_ish.add_argument(
        "-s", "--shell", choices=["zsh", "bash"], default=None, help="override shell detection"
    )
    p_ish.add_argument(
        "-w",
        "--wrapper-name",
        default="c",
        metavar="NAME",
        help="name for the AIM-at-startup wrapper function (default: c)",
    )
    p_ish.add_argument(
        "-W", "--no-wrapper", action="store_true", help="skip the AIM-at-startup wrapper piece"
    )
    p_ish.add_argument(
        "-B", "--no-badges", action="store_true", help="skip the cross-terminal tab-badge piece"
    )
    p_ish.add_argument(
        "-n", "--dry-run", action="store_true", help="print the block + target; write nothing"
    )
    p_ish.add_argument(
        "-u", "--uninstall", action="store_true", help="remove only the ccc block from the rc"
    )
    p_ish.set_defaults(func=cmd_install_shell)

    p_obs = sub.add_parser(
        "obsidian-setup",
        help="seed the vault's task folders, dashboards & job-button shellcommands",
        description=(
            "Create the ccc task-folder structure (future/delete/running/done/sessions + the "
            "capture pad) derived from config, render the four generified Obsidian dashboards "
            "from templates (folder paths + the abs ccc binary come from config, not "
            "hardcoded), and merge the obsidian-shellcommands entries the in-note job buttons "
            "fire. Default run is content-only and offline; generated dashboards carry a "
            "ccc_generated marker so reruns/-u only touch ccc's own files. "
            "--install-plugins is the only networked path (consent-gated, sha256-verified)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_obs.add_argument(
        "-r", "--root", default=None, help="target vault (default: the vault_root config)"
    )
    p_obs.add_argument(
        "-n", "--dry-run", action="store_true", help="show what would change; write nothing"
    )
    p_obs.add_argument(
        "-u", "--uninstall", action="store_true", help="remove ccc-generated dashboards + entries"
    )
    p_obs.add_argument(
        "-p",
        "--install-plugins",
        action="store_true",
        help="also download+enable the three pinned community plugins (network; consent-gated)",
    )
    p_obs.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="assume yes to the --install-plugins consent prompt (non-interactive)",
    )
    p_obs.set_defaults(func=cmd_obsidian_setup)

    p_init = sub.add_parser(
        "init",
        help="first-run wizard: environment check, config, and the installers",
        description=(
            "Interactive first-run setup. Detects the environment, asks for a vault path, "
            "walks a consent checklist (LLM checkers recommended ON; vault features when a "
            "vault is given; copilot/resume/reap individually), writes a minimal config.toml "
            "(only keys differing from the defaults), then offers to run install-hooks, "
            "install-statusline, install-commands, obsidian-setup and the daemon agent, and "
            "finishes with `ccc doctor`. -y writes the recommended profile non-interactively; "
            "-m writes a minimal no-features config and runs no installers. No LLM/API calls."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_init.add_argument(
        "-y", "--yes", action="store_true", help="recommended profile, non-interactive"
    )
    p_init.add_argument(
        "-m", "--minimal", action="store_true", help="write nothing on; run no installers"
    )
    p_init.add_argument(
        "-r", "--vault-root", default=None, help="vault path (for -y/-m; enables vault features)"
    )
    p_init.add_argument(
        "-f", "--force", action="store_true", help="overwrite an existing config (backed up first)"
    )
    p_init.set_defaults(func=cmd_init)

    p_daemon = sub.add_parser(
        "daemon",
        help="background housekeeper: reap idle, done-check, summaries, sub-goals, alerts",
        description=(
            "The daemon is the command center's background housekeeper. One pass "
            "reaps idle interactive sessions (SIGTERM; transcript kept — resume by id), "
            "runs done-check commands, regenerates summaries / next-steps, auto-derives "
            "and ticks sub-goals, and fires deadline / stale alerts. Run it periodically "
            "via a launchd agent (installed below), or one-shot / --loop by hand."
        ),
        epilog=(
            "control:\n"
            "  ccc daemon                  run one pass right now\n"
            "  ccc daemon --dry-run -v     preview what a pass would do\n"
            "  ccc daemon --loop           run continuously in this terminal\n"
            "  ccc daemon --install        install + start the service (launchd/systemd)\n"
            "  ccc daemon --uninstall      stop + remove the service\n"
            "  ccc daemon --status         is the recurring service running?\n"
            "\n"
            "recurring service: launchd agent (macOS) or systemd --user timer (Linux).\n"
            "is it running / where are the logs:\n"
            "  ccc daemon --status\n"
            "  ~/.claude/command-center/daemon.log   (and daemon.err)\n"
            "\n"
            "tunables (interval, reaper on/off, auto-progress) live in the TUI Settings\n"
            "screen (press 's') or ~/.claude/command-center/config.toml."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_daemon.add_argument("--once", action="store_true", help="run a single pass (the default)")
    p_daemon.add_argument("--loop", action="store_true", help="run continuously")
    p_daemon.add_argument("--interval", type=int, help="seconds between passes in --loop")
    p_daemon.add_argument("--dry-run", action="store_true", help="show actions without doing them")
    p_daemon.add_argument("--no-reap", action="store_true", help="skip auto-closing idle sessions")
    p_daemon.add_argument("--no-summary", action="store_true", help="skip LLM summary regeneration")
    p_daemon.add_argument(
        "--no-progress", action="store_true", help="skip auto-derive/auto-check of sub-goals"
    )
    p_daemon.add_argument("--no-alerts", action="store_true", help="skip deadline/stale alerts")
    p_daemon.add_argument("-v", "--verbose", action="store_true", help="list affected session ids")
    p_daemon.add_argument(
        "--install",
        action="store_true",
        help="install + start the recurring service (launchd on macOS, systemd --user on Linux)",
    )
    p_daemon.add_argument(
        "--uninstall", action="store_true", help="stop + remove the recurring service"
    )
    p_daemon.add_argument(
        "--status", action="store_true", help="report whether the recurring service is running"
    )
    p_daemon.set_defaults(func=cmd_daemon)

    p_rh = sub.add_parser(
        "resume-halted",
        help="auto-resume session-limit-halted sessions once the limit resets",
        description=(
            "Resume sessions stalled on a Claude rate limit, once the limit resets, "
            "via claude-session-continue.py — staggered ~2 min across repos and strictly "
            "serial within a repo. Default ON (config resume_halted); normally spawned "
            "automatically by the daemon. Run --watch by hand to drive it in a terminal, "
            "or --dry-run to preview the plan."
        ),
        epilog=(
            "examples:\n"
            "  ccc resume-halted --dry-run   show halted candidates + the planned actions\n"
            "  ccc resume-halted             run one orchestration tick now\n"
            "  ccc resume-halted --watch     run the singleton poll loop (until drained)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_rh.add_argument("--watch", action="store_true", help="run the singleton poll loop")
    p_rh.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p_rh.add_argument(
        "--stagger", type=int, default=0, metavar="SEC", help="override resume_stagger_sec"
    )
    p_rh.set_defaults(func=cmd_resume_halted)

    p_ap = sub.add_parser(
        "autoprogress", help="auto-derive + auto-check sub-goals for AIM sessions (cheap LLM)"
    )
    p_ap.add_argument("--session", help="run a single session (default: the capped daemon pass)")
    p_ap.add_argument("--dry-run", action="store_true", help="propose without writing any checks")
    p_ap.add_argument("-v", "--verbose", action="store_true", help="also print skip reasons")
    p_ap.set_defaults(func=cmd_autoprogress)

    p_sym = sub.add_parser(
        "tab-symbol",
        help="print a repo's deterministic badge, or claim this iTerm tab's badge",
        description=(
            "Prints a distinct colored emoji for a repo. With --print [PATH] it prints "
            "the DETERMINISTIC per-repo symbol for PATH (or the cwd) — no iTerm needed, so "
            "the cross-terminal shell badge hook (ccc install-shell) uses it on Linux and "
            "any plain terminal. Without --print it assigns/reads the current iTerm tab's "
            "unique badge ($ITERM_SESSION_ID), idempotent per tab, persisted under "
            "~/.cache/iterm-tab-symbol/, so the tab title and the command-center row show "
            "the same badge to tell same-folder sessions apart."
        ),
    )
    p_sym.add_argument("path", nargs="?", help="path to symbol for --print (default: cwd)")
    p_sym.add_argument(
        "-p",
        "--print",
        dest="print_only",
        action="store_true",
        help="print PATH's deterministic per-repo symbol (no iTerm); for the shell hook",
    )
    p_sym.add_argument(
        "-c",
        "--color",
        action="store_true",
        help="with --print, also print the repo's colour (hex/name)",
    )
    p_sym.add_argument("-i", "--session-id", help="override $ITERM_SESSION_ID (the tab key)")
    p_sym.add_argument(
        "-r", "--read", action="store_true", help="print the existing badge only; do not assign"
    )
    p_sym.add_argument(
        "-S",
        "--sync",
        action="store_true",
        help="re-apply every tracked live tab's '<emoji> repo' title via AppleScript "
        "(badge tabs that were already open before the hook landed)",
    )
    p_sym.set_defaults(func=cmd_tab_symbol)

    p_peek = sub.add_parser(
        "peek",
        help="show my last prompt for the iTerm-focused session in a floating panel",
        description=(
            "Maps the focused iTerm tab to the Claude session running in it and shows "
            "that session's last human prompt in a floating macOS panel that closes on "
            "Space, Return or Escape. Bound to a Karabiner chord (hold s, tap p). "
            "Pass --session <id> to peek a specific session directly (bypassing focus "
            "detection) — the ccc TUI's sp chord uses this for the highlighted row."
        ),
    )
    p_peek.add_argument(
        "-s",
        "--session",
        metavar="ID",
        default=None,
        help="peek this exact tracked session id, skipping iTerm focus detection",
    )
    p_peek.add_argument(
        "-p",
        "--print",
        dest="print_only",
        action="store_true",
        help="print the resolved prompt to stdout instead of showing the panel",
    )
    p_peek.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=0.0,
        help="auto-close the panel after N seconds (0 = wait for a key; for smoke tests)",
    )
    p_peek.set_defaults(func=cmd_peek)

    p_jump = sub.add_parser(
        "jump",
        help="toggle between the ccc TUI and a session tab (bound to a Karabiner chord)",
        description=(
            "Context-aware toggle. From a Claude session tab: select that session's row "
            "in the live TUI, then bring the ccc tab forward. From the ccc tab: jump to "
            "the selected session's tab (like the TUI's `r`). From another app: just "
            "bring ccc forward. The TUI is found by its controlling tty (title-"
            "independent), falling back to the configured tab title, then to opening one."
        ),
    )
    p_jump.add_argument(
        "--no-toggle",
        action="store_true",
        help="always just focus the ccc TUI (skip the session-tab/ccc toggle behavior)",
    )
    p_jump.add_argument(
        "--no-launch",
        action="store_true",
        help="if no ccc TUI tab exists, fail instead of opening a new one",
    )
    p_jump.set_defaults(func=cmd_jump)

    p_snap = sub.add_parser(
        "snapshot",
        help="save the whole iTerm layout (windows/tabs/splits + what each pane runs)",
        description=(
            "Capture every iTerm2 window, its tabs and each tab's split topology, plus "
            "what each pane is doing: a tracked Claude session (id + account), a "
            "foreground program with its exact argv, or just a shell at its cwd. The "
            "snapshot is a 0600 JSON file under CLAUDE_HOME; `ccc restore-snapshot` "
            "rebuilds it after a reboot. Requires macOS + iTerm2 with 'Enable Python "
            "API' on (there is no tmux/AppleScript fallback in v1)."
        ),
    )
    p_snap.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="list saved snapshots (name, age, window/tab/pane counts) instead of capturing",
    )
    p_snap.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print the capture and its per-pane classification without writing a file",
    )
    p_snap.set_defaults(func=cmd_snapshot)

    p_restore = sub.add_parser(
        "restore-snapshot",
        help="rebuild a saved iTerm layout (resume the Claude sessions, re-run the rest)",
        description=(
            "Rebuild the windows/tabs/splits of a snapshot: every Claude session is "
            "resumed on ITS OWN account (skipped when already live, refused when its "
            "resume is blocked), every other pane re-runs its captured argv when the "
            "program is on the `snapshot_restore_commands` allowlist, else lands as a "
            "shell at the recorded cwd printing what used to run there. Defaults to the "
            "newest snapshot; a bare name resolves in the snapshots dir (with or without "
            "the .json), anything containing '/' is used as a path."
        ),
    )
    p_restore.add_argument(
        "name", nargs="?", default=None, help="snapshot name or path (default: the newest)"
    )
    p_restore.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print the full window/tab/pane plan without touching iTerm2",
    )
    p_restore.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="restore even though iTerm2 already has other panes open (duplicates the layout)",
    )
    p_restore.set_defaults(func=cmd_restore_snapshot)

    p_demo = sub.add_parser(
        "demo",
        help="seed a throwaway fake-data store and open the TUI against it (safe to try)",
        description=(
            "Populate a self-contained demo home (never the real CLAUDE_HOME) with ~10 "
            "deterministic fake sessions — varied statuses, AIMs + scores, progress bars, a "
            "FUTURE + a SCHEDULED job — then launch the TUI against it. --ls prints `ccc ls` "
            "instead; -d/--dir sets the demo home; -x/--clean deletes it. Spends no LLM "
            "tokens and touches nothing outside the demo dir."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_demo.add_argument(
        "-l", "--ls", action="store_true", help="print `ccc ls` instead of launching the TUI"
    )
    p_demo.add_argument(
        "-d", "--dir", default=None, help="demo home dir (default: ~/.cache/ccc-demo)"
    )
    p_demo.add_argument("-x", "--clean", action="store_true", help="delete the demo dir and exit")
    p_demo.set_defaults(func=cmd_demo)

    sub.add_parser("tui", help="interactive Textual command center").set_defaults(func=cmd_tui)

    p_serve = sub.add_parser("serve", help="serve the TUI in the browser (textual-serve)")
    p_serve.add_argument("--host", default="127.0.0.1", help="bind host (default localhost)")
    p_serve.add_argument("--port", type=int, default=8000, help="bind port (default 8000)")
    p_serve.set_defaults(func=cmd_serve)

    p_tag = sub.add_parser("tag", help="manage typed/colored @tags (list/add/type)")
    tagsub = p_tag.add_subparsers(dest="tagcmd")
    tagsub.add_parser("list", help="list defined tags and types")
    p_tag_add = tagsub.add_parser("add", help="assign a tag to a type, e.g. tag add @susi people")
    p_tag_add.add_argument("name")
    p_tag_add.add_argument("type")
    p_tag_type = tagsub.add_parser(
        "type", help="define a type→color, e.g. tag type place '#5599ff'"
    )
    p_tag_type.add_argument("name")
    p_tag_type.add_argument("color")
    p_tag.set_defaults(func=cmd_tag)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # Default surface: the interactive TUI in a terminal, the flat list when piped.
        return cmd_tui(args) if sys.stdout.isatty() else cmd_ls(args)
    func = args.func
    return int(func(args))
