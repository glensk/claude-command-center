#!/usr/bin/env python3
"""Claude Code hook handlers, invoked as ``ccc hook <event>``.

Each handler reads the hook's JSON payload from stdin. Two hard rules:

* **Never raise** — a crashing hook would disrupt every Claude Code turn, so the
  dispatcher swallows all exceptions and always exits 0.
* **Stay fast and LLM-free** — summary / next-step regeneration is done
  out-of-band by the daemon (phase 3); hooks only touch the local SQLite store.

``SessionStart`` / ``UserPromptSubmit`` may inject ``additionalContext`` to make
Claude ask for (or be reminded of) the session's done-condition.
"""

# Handlers lazy-import heavier deps (adapter, spawn) to keep the hook path light.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import json
import os
import sys
import time
from typing import Any

from . import config
from .models import Session, Status, drift_unresolved, dumps_todos, now_ms, short_id
from .store import Store

_ASK_AIM = (
    "This Claude Code session has no done-condition recorded yet. Before doing "
    "anything else, ask the user: \"What does 'done' look like for this "
    'session?" When they answer, save it by running this shell command:\n'
    '    ccc set-aim --session {sid} "<their answer>"\n'
    "then propose 3-6 concrete sub-goals and save them so progress can be "
    "tracked:\n"
    '    ccc subgoals --session {sid} "first step" "second step" ...'
)

_GROUND = "Session done-condition (AIM): {aim}{progress}{nxt}"

_NUDGE = (
    "Reminder: this session still has no done-condition. Ask the user what "
    "'done' looks like, then run: ccc set-aim --session {sid} \"...\""
)

_SHARPEN_AIM = (
    'Note — this session\'s AIM ("{aim}") scores {score}/100 on concreteness, below the '
    "{threshold} bar, so progress can't be graded. {why}"
    "After handling my request this turn, sharpen the AIM yourself — KEEP my underlying goal "
    "intact, only make it concrete and verifiable. Ground the rewrite in what THIS session has "
    "actually been doing: the files you've edited, your current TodoWrite list, and the task in "
    "progress. Verify each candidate against the INDEPENDENT checker (do not self-judge):\n"
    '    ccc score-aim --dry-run "<candidate>"\n'
    'It returns a 0-100 score and a "missing" hint; revise until score >= {threshold}, then '
    "apply it yourself:\n"
    '    ccc set-aim --session {sid} "<your sharpened AIM>"\n'
    'Then tell me old -> new and the one-line revert: ccc set-aim --session {sid} "{aim}". '
    'Report the result as a "🔴 **AIM sharpened** (was {score}/100):" line so it visibly '
    "stands out (Claude Code can't colour chat text, so the marker substitutes for red). "
    "Never change what the goal MEANS — only how concretely it is stated."
)


_ADAPT_SUBGOALS = (
    'Your AIM was updated to: "{aim}" (revision {rev}), but the sub-goal checklist still '
    "tracks an earlier AIM (v{old_rev}). Re-align it to the CURRENT AIM, preserving progress: "
    "keep the wording of any still-valid item IDENTICAL (so its tick carries over), drop items the "
    "new AIM no longer needs, and add any newly-required ones. Save with:\n"
    '    ccc subgoals --session {sid} --merge --source agent "step one" "step two" ...\n'
    "Keep each item concrete and objectively checkable. An impartial checker will then review the "
    "change for drift, so do not quietly drop scope or weaken a goal."
)


_TICK_SUBGOALS = (
    "This session's progress checklist is partly done but has unticked items — your recent work "
    "may have completed some. Tick every sub-goal you have ACTUALLY finished (leave the rest):\n"
    "{items}\n"
    "Tick with: ccc check --session {sid} <N>  (the number shown above; --uncheck to revert). "
    "The auto-grader is deliberately conservative and under-counts, so YOU are the authoritative "
    "judge of what is done — don't leave finished work unticked."
)


_DRIFT_NUDGE = (
    "An IMPARTIAL checker flagged your latest sub-goal change as DRIFT ({severity}): {reason} "
    "Reconcile NOW: restore any dropped coverage and strengthen any weakened goal so the checklist "
    "again means the AIM is done. If the change was genuinely legitimate, acknowledge it with "
    "ccc ack-drift --session {sid}. Do not ignore this — it is how the session is kept on goal."
)


_LOCK_DENY = (
    "`{path}` is being edited by another live Claude Code session ({holder}). You're queued for "
    "it — edit a DIFFERENT file now, or retry this one shortly: it frees automatically when they "
    "finish (or when they run `ccc handoff` there), and your edit will then go through."
)

_HANDOFF_NUDGE = (
    "Another Claude Code session is waiting to edit {files}. If you are DONE with {it} for this "
    "turn, release {it} now so they can start — this commits, pushes, then unlocks:\n{cmds}\n"
    "If you still need to edit {it} this turn, just keep going; {it} releases automatically when "
    "your turn ends."
)

_PARKED_PROMPT = (
    "⏳ A parked prompt is ARMED for this session (auto-delivery {fire}): «{first}». "
    "If the user asks to run or continue it now (a bare 'continue' counts): run "
    "`ccc claim-fire {sid}` FIRST — exit 0 prints the full prompt text and hands "
    "delivery to you (execute that text as if the user had just typed it); a non-zero "
    "exit means it was already delivered or disarmed — then do NOT run it. For any "
    "other request, leave it armed and just answer."
)


_LOCK_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# How long a `mark-done --close` close-after-turn request stays claimable (10 min). A
# stamp older than this is cleared but never fired — so a stale arm can't survive into a
# resumed session (see store.claim_close_request).
CLOSE_REQUEST_TTL_MS = 10 * 60 * 1000


def _parked_prompt_notice(session: Session) -> str | None:
    """The armed-attached-prompt notice for *session*, or ``None`` when nothing is armed.

    Makes a bare "continue" work: the user parks the real prompt ON the session
    (``ccc park -g`` attach mode) and may address it before the daemon's auto-fire
    delivers it. The `ccc claim-fire` claim is one-shot, so the session executing
    it now and the daemon delivering at the reset can never both run it.
    """
    prompt = (session.prompt or "").strip()
    if not session.fire_at or not prompt:
        return None
    from . import park  # lazy: keep the hook import light

    first = prompt.splitlines()[0][:150] + ("…" if len(prompt) > 150 or "\n" in prompt else "")
    return _PARKED_PROMPT.format(
        fire=park.format_fire(session.fire_at), first=first, sid=session.session_id
    )


def _sharpen_context(session: Session, sid: str, threshold: int) -> str:
    """The vague-AIM nudge, filled with the session's current score + checker reason."""
    why = f"The checker's reason: {session.aim_score_reason}. " if session.aim_score_reason else ""
    return _SHARPEN_AIM.format(
        aim=session.aim, sid=sid, score=session.aim_score, threshold=threshold, why=why
    )


def _maybe_grade_after_turn(session_id: str, last_progress_at: int) -> None:
    """Spawn a detached after-turn grader, debounced. Never blocks or raises.

    Skipped inside our own ``claude -p`` (``CCC_INTERNAL`` set, so the grader's own
    Stop hook can't recurse), when disabled, or when a grade ran within
    ``grade_debounce_sec``. The grader stamps ``last_progress_at`` itself (only on a
    real grading pass), so a no-op spawn never debounces out a genuine one.
    """
    if os.environ.get("CCC_INTERNAL"):
        return
    cfg = config.load_config()
    if not cfg.grade_on_turn:
        return
    if now_ms() - last_progress_at < cfg.grade_debounce_sec * 1000:
        return
    from . import spawn  # lazy: keep the hook import light

    spawn.spawn_ccc(["autoprogress", "--session", session_id])


def _maybe_assess_aim_after_turn(session: Session) -> None:
    """Spawn a detached "is the AIM fulfilled?" self-assessment after a turn. Never blocks/raises.

    Skipped inside our own ``claude -p`` (``CCC_INTERNAL``, so the checker's own Stop hook can't
    recurse), when disabled, and for ineligible sessions (no concrete AIM / draft / done /
    archived). ``ccc assess-aim`` then enforces the new-turn gate, transcript-exists and the
    stale-write guard authoritatively — this only avoids a pointless spawn.
    """
    if os.environ.get("CCC_INTERNAL"):
        return
    cfg = config.load_config()
    if not cfg.assess_aim_on_turn:
        return
    from . import aimmet  # lazy: keep the hook import light

    if not aimmet.eligible(session, cfg):
        return
    from . import spawn

    spawn.spawn_ccc(["assess-aim", "--session", session.session_id])


_ACCOUNT_SWITCH = (
    "Heads up: this session last ran under the {old} Claude account, but this shell is "
    "billing the {new} account (CLAUDE_CONFIG_DIR). If that is not what you intend, exit "
    "and re-open it from the right account — the native `claude --resume` "
    "picker does not pin an account, so ccc cannot correct this for you."
)


def ensure_current_session(store: Store, sid: str, cwd: str) -> tuple[Session, str | None]:
    """Ensure *sid*'s row and stamp its account from the in-session env (R4/D11).

    Hooks run INSIDE a Claude session, so ``CLAUDE_CONFIG_DIR`` is authoritative for
    which account this shell bills. Returns ``(session, switch_warning)``: the warning
    is set (for the SessionStart hook to surface via ``additionalContext``) when the
    row's LAST-observed account differs from this shell's — the D11 account-switch guard,
    the only signal that reaches the native ``claude --resume`` picker. An empty stored
    ``config_dir`` means the default account (all rows are backfilled), so a first run
    under the default account never warns.
    """
    from . import accounts

    current = accounts.env_config_dir()
    existing = store.get(sid)
    warning: str | None = None
    if existing is None:
        # Brand-new row: there is no PRIOR account to have switched from — never warn.
        session = store.ensure(sid, cwd=cwd)
    else:
        session = existing
        stored = session.config_dir or str(accounts.default_config_dir())
        if not accounts.same_config_dir(stored, current):
            warning = _ACCOUNT_SWITCH.format(
                old=accounts.account_label(stored), new=accounts.account_label(current)
            )
    if current and current != (session.config_dir or ""):
        store.update_fields(sid, config_dir=current)
        session = store.get(sid) or session
    # Track the CURRENT iTerm tab on EVERY hook event, not only SessionStart: the
    # stamp used to be SessionStart-only, so one missed start (a transient store
    # error mid-hook, or the row first created by another handler) left the session
    # permanently un-locatable — ``f+j`` could neither highlight its row nor focus
    # its tab ("Session is live but its tab can't be located"), and the TUI folder
    # badge fell back to the per-repo symbol while the tab showed its claimed one.
    # Re-stamping here self-heals within one hook event and also follows a session
    # that lands in a new tab. An absent env var (headless/daemon) never clears.
    iterm = os.environ.get("ITERM_SESSION_ID")
    if iterm and iterm != (session.iterm_session_id or ""):
        store.update_fields(sid, iterm_session_id=iterm)
        session = store.get(sid) or session
    return session, warning


def _read_payload() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _session_id(payload: dict[str, Any]) -> str | None:
    sid = payload.get("session_id") or payload.get("sessionId")
    return sid if isinstance(sid, str) else None


def _todos_from_payload(payload: dict[str, Any]) -> list[tuple[str, str]] | None:
    """Extract a TodoWrite-style list (``tool_input.todos``) from a PostToolUse payload.

    Returns ``None`` when the tool call carries no full todo list (e.g. ``TaskUpdate``
    touches a single task) so the caller can fall back to the on-disk snapshot; an
    explicit (possibly empty) list otherwise.
    """
    tool_input = payload.get("tool_input")
    raw = tool_input.get("todos") if isinstance(tool_input, dict) else None
    if not isinstance(raw, list):
        return None
    todos: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        subject = item.get("content") or item.get("subject") or item.get("activeForm") or ""
        if subject:
            todos.append((str(item.get("status", "pending")), str(subject)))
    return todos


def _emit_context(event: str, context: str | None) -> None:
    if not context:
        return
    json.dump(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}},
        sys.stdout,
    )
    sys.stdout.write("\n")


def _lock_path(payload: dict[str, Any]) -> str | None:
    """The absolute file path an Edit/Write/NotebookEdit tool call targets, if any."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    return str(path) if path else None


def _live_ids() -> set[str]:
    """Session ids currently alive. Fail-open: any error → empty set → nothing blocks."""
    try:
        from .adapters.claude import ClaudeAdapter

        return {ls.session_id for ls in ClaudeAdapter().discover() if ls.alive}
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return set()


def _emit_pre_tool_deny(reason: str) -> None:
    """Emit the current-schema PreToolUse deny decision (exit 0, decision honoured)."""
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


def _handoff_nudge(files: list[str]) -> str:
    cmds = "\n".join(f"    ccc handoff {path}" for path in files)
    listed = ", ".join(f"`{path}`" for path in files)
    return _HANDOFF_NUDGE.format(files=listed, it="it" if len(files) == 1 else "them", cmds=cmds)


def _run_done_check(command: str, cwd: str) -> bool:
    """Return True if the configured done-check command exits 0."""
    from . import checks

    return checks.run_exit0(command, cwd, timeout=30)


def _switch_landed_as_expected(store: Store, sid: str) -> bool:
    """Consume a claimed ``switch-account`` expectation; True when this start matches it.

    An intentional relaunch under the expected account makes the "last ran under another
    account" heads-up noise. The expectation is consumed either way — a start under some
    OTHER account keeps its warning.
    """
    expected = store.pop_switch_expectation(sid)
    if not expected:
        return False
    from . import accounts  # lazy, like ensure_current_session

    return accounts.same_config_dir(expected, accounts.env_config_dir())


def handle_session_start(payload: dict[str, Any]) -> int:
    sid = _session_id(payload)
    if not sid:
        return 0
    with Store() as store:
        session, switch_warning = ensure_current_session(store, sid, payload.get("cwd", ""))
        # A fresh (or resumed) session has no in-flight subagents: whatever the previous
        # incarnation was running died with it and never fired its SubagentStop, and a
        # stranded count would veto every `switch-account` for this id from here on.
        store.reset_subagents(sid)
        if switch_warning and _switch_landed_as_expected(store, sid):
            switch_warning = None
        # The iTerm tab id itself is stamped inside ensure_current_session (every
        # hook event, self-healing) — here we only need the env value for the badge.
        iterm = os.environ.get("ITERM_SESSION_ID")
        # Seed this tab's distinctness badge into its title now, so a freshly-launched
        # (or resumed) session's tab shows the same emoji as its command-center row
        # without waiting for the next `cd` (zsh hook) or daemon pass. Marker-preserving
        # and fail-safe (detached AppleScript that swallows its own errors).
        if iterm:
            from . import tabsymbol  # lazy: keep the hook import light

            tabsymbol.seed_title(iterm, session.cwd or payload.get("cwd", ""))
        env_aim = os.environ.get("CLAUDE_SESSION_AIM")
        if env_aim and env_aim.strip() and not session.aim:
            # Go through set_aim (not a bare update_fields) so the AIM is scored on
            # arrival — a bare write left aim_score at -1, so a hook-seeded AIM was
            # never graded for vagueness. set_aim sets the instant lexical score;
            # the detached score-aim then refines it via one cheap LLM call.
            if store.set_aim(sid, env_aim.strip()) and config.load_config().aim_score_on_set:
                from . import spawn  # lazy: keep the hook import light

                spawn.spawn_ccc(["score-aim", "--session", sid])
            session = store.get(sid) or session
        if not session.aim:
            context = _ASK_AIM.format(sid=sid)
        else:
            checked, total = store.progress(sid)
            progress = f"\nProgress: {checked}/{total} sub-goals done." if total else ""
            nxt = f"\nAgreed next step: {session.next_step}" if session.next_step else ""
            context = _GROUND.format(aim=session.aim, progress=progress, nxt=nxt)
            # A vague AIM can't be graded — ask the agent to sharpen it first thing.
            threshold = config.load_config().aim_score_threshold
            if 0 <= session.aim_score < threshold:
                context += "\n" + _sharpen_context(session, sid, threshold)
        # An armed parked prompt must be visible from the first turn — a resumed
        # session otherwise guesses from the AIM while the real prompt sits armed.
        context = "\n\n".join(filter(None, [context, _parked_prompt_notice(session)]))
        # D11: prepend the account-switch warning (the only guard the native --resume
        # picker reaches) so a session resumed under the wrong account says so up front.
        if switch_warning:
            context = f"{switch_warning}\n\n{context}"
        _emit_context("SessionStart", context)
    return 0


def handle_user_prompt(payload: dict[str, Any]) -> int:
    sid = _session_id(payload)
    if not sid:
        return 0
    with Store() as store:
        session, _ = ensure_current_session(store, sid, payload.get("cwd", ""))
        count = session.prompt_count + 1
        store.update_fields(sid, last_response_at=now_ms(), prompt_count=count)
        # A new turn begins: clear any "AIM just changed" marker so the status-line
        # transition (old ====> new) is scoped to the turn it changed in.
        if session.aim_prev:
            store.update_fields(sid, aim_prev=None)
        cfg = config.load_config()
        # AIM-related nudges are mutually exclusive (no-aim > vague > stale checklist);
        # an unresolved drift nudge is additive — it can fire alongside any of them.
        parts: list[str] = []
        if not session.aim:
            every = cfg.nag_every_n_turns
            if every > 0 and (count - 1) % every == 0:
                parts.append(_NUDGE.format(sid=sid))
        elif 0 <= session.aim_score < cfg.aim_score_threshold:
            every = cfg.sharpen_every_n_turns
            if every > 0 and (count - 1) % every == 0:
                parts.append(_sharpen_context(session, sid, cfg.aim_score_threshold))
        elif cfg.adapt_subgoals_on_aim_change and store.subgoals_stale(sid):
            # AIM is concrete but the adaptive checklist still tracks an older AIM: nudge the
            # agent to merge-regenerate it (fires every turn until it does — staleness clears it).
            parts.append(
                _ADAPT_SUBGOALS.format(
                    aim=session.aim,
                    sid=sid,
                    rev=store.count_aim_history(sid),
                    old_rev=session.subgoals_aim_rev,
                )
            )
        elif cfg.nudge_unchecked_every_n_turns > 0 and (
            (count - 1) % cfg.nudge_unchecked_every_n_turns == 0
        ):
            # AIM is concrete and the checklist is aligned, but it is partly done with items left
            # unticked — the agent likely finished some without ticking. Only nudge on a partial
            # checklist (0 < checked < total) so a fresh 0/N list isn't nagged before any work.
            subs = store.list_subgoals(sid)
            checked = sum(1 for s in subs if s.checked)
            if subs and 0 < checked < len(subs):
                items = "\n".join(f"  {s.position + 1}. {s.text}" for s in subs if not s.checked)
                parts.append(_TICK_SUBGOALS.format(items=items, sid=sid))
        if drift_unresolved(session):
            parts.append(
                _DRIFT_NUDGE.format(
                    severity=session.drift_severity,
                    reason=session.drift_reason or "(see ccc subgoal-history)",
                    sid=sid,
                )
            )
        # Armed parked prompt: additive on every turn while armed, so a bare
        # "continue" typed into the session resolves to the parked prompt.
        parked = _parked_prompt_notice(session)
        if parked:
            parts.append(parked)
        if parts:
            _emit_context("UserPromptSubmit", "\n\n".join(parts))
    return 0


def handle_stop(payload: dict[str, Any]) -> int:
    sid = _session_id(payload)
    if not sid:
        return 0
    with Store() as store:
        session, _ = ensure_current_session(store, sid, payload.get("cwd", ""))
        # Keep this fast: just record activity and flag a summary refresh for the daemon.
        store.update_fields(sid, last_response_at=now_ms(), needs_summary=True)
    # Grade this turn's progress out-of-band (detached, debounced) — never blocks here.
    _maybe_grade_after_turn(sid, session.last_progress_at)
    # Self-assess whether the AIM is now fulfilled (detached; the red DONE in the bar).
    _maybe_assess_aim_after_turn(session)
    return 0


def handle_pre_tool_use(payload: dict[str, Any]) -> int:
    """Acquire the cross-session lock on the file an Edit/Write targets, or deny + queue.

    Fail-open by construction: returns 0 (no decision → the edit proceeds) when the file is
    free / already ours / reclaimable, when locking is disabled or no file path is present,
    and — because ``dispatch`` swallows exceptions — on any error. The single non-proceed
    outcome is an explicit *deny* when the file is held by another **live** session (and any
    configured ``file_lock_wait_sec`` poll grace has elapsed).
    """
    cfg = config.load_config()
    if not cfg.file_lock_enabled:
        return 0
    sid = _session_id(payload)
    path = _lock_path(payload)
    if not sid or not path:
        return 0
    ttl_ms = cfg.file_lock_ttl_sec * 1000
    with Store() as store:
        ensure_current_session(store, sid, payload.get("cwd", ""))
        holder = store.acquire_file_lock(sid, path, now_ms(), _live_ids(), ttl_ms)
        if holder is None:
            return 0  # acquired / refreshed → let the edit run
        store.add_waiter(sid, path, now_ms())
        deadline = now_ms() + cfg.file_lock_wait_sec * 1000
        while now_ms() < deadline:  # optional poll-then-deny grace (default 0 → deny at once)
            time.sleep(0.5)
            holder = store.acquire_file_lock(sid, path, now_ms(), _live_ids(), ttl_ms)
            if holder is None:
                return 0
    _emit_pre_tool_deny(_LOCK_DENY.format(path=path, holder=short_id(holder)))
    return 0


def handle_post_tool_use(payload: dict[str, Any]) -> int:
    """After a file edit: nudge a hand-off if a peer is queued. After a todo/task tool:
    snapshot the live todo list. (One handler, wired for both tool groups.)"""
    if (payload.get("tool_name") or "") in _LOCK_TOOLS:
        return _post_tool_edit(payload)
    return _post_tool_todos(payload)


def _post_tool_edit(payload: dict[str, Any]) -> int:
    """A file edit just completed — if another session is queued on a file we hold, tell us
    to hand it off (the agent decides; it is the only party that knows it is done with it)."""
    cfg = config.load_config()
    if not cfg.file_lock_enabled:
        return 0
    sid = _session_id(payload)
    if not sid:
        return 0
    with Store() as store:
        waiters = store.waiters_on_my_locks(sid)
    files = sorted({w.file_path for w in waiters})
    if files:
        _emit_context("PostToolUse", _handoff_nudge(files))
    return 0


def _post_tool_todos(payload: dict[str, Any]) -> int:
    """Snapshot the session's live todo list into the store (TodoWrite / Task tools).

    Fires after every matched todo/task tool call, so the command center always
    has the current ``done / to-do`` picture for the session, forwarded the moment
    it changes. Source order: the tool payload's ``tool_input.todos`` (TodoWrite),
    then the on-disk ``~/.claude/tasks/<id>/`` list (Task tools). Fast and LLM-free.
    """
    sid = _session_id(payload)
    if not sid:
        return 0
    todos = _todos_from_payload(payload)
    if todos is None:
        from . import accounts  # lazy: keep other hooks light
        from .adapters.claude import ClaudeAdapter

        todos = ClaudeAdapter().todos(sid, accounts.env_config_dir())
    with Store() as store:
        ensure_current_session(store, sid, payload.get("cwd", ""))
        store.update_fields(sid, todos=dumps_todos(todos), todos_updated_at=now_ms())
    return 0


def handle_release_locks(payload: dict[str, Any]) -> int:
    """Release all of the session's file locks — the Stop floor.

    Wired as the final Stop hook, *after* the auto-commit, so the session's files are already
    committed + pushed before their locks drop. A parked / idle session therefore holds none.

    A ``mark-done --close`` may also have armed a one-shot close-after-turn request: claim it
    atomically (at most one caller ever wins) and, on success, spawn the detached closer.
    Running last means the SIGTERM + pane/tab close happen only after auto-commit committed
    this turn's work. Kept fast and never-raising (dispatch swallows too, but we mirror the
    defensive neighbours).
    """
    sid = _session_id(payload)
    if sid:
        with Store() as store:
            store.release_all_file_locks(sid)
            kind, claim = store.claim_after_turn(sid, now_ms(), CLOSE_REQUEST_TTL_MS)
            if kind == "close":
                from . import spawn  # lazy, like _maybe_grade_after_turn

                spawn.spawn_ccc(
                    [
                        "close-now",
                        "--session",
                        sid,
                        "--iterm",
                        os.environ.get("ITERM_SESSION_ID", ""),
                    ]
                )
            elif kind == "switch" and claim is not None:
                # `ccc switch-account`: the relauncher terminates this Claude once its Stop
                # chain is over, waits for the shell to come back and types the resume into
                # this very tab under the target account's env pin. Everything the launch
                # depends on travels in argv — the claimed snapshot plus this hook's own env
                # (fresh iTerm/tmux ids, $CLAUDE_PID, the account this process bills).
                from . import accounts, spawn  # lazy, like _maybe_grade_after_turn

                args = [
                    "switch-now",
                    "--session",
                    sid,
                    "--iterm",
                    os.environ.get("ITERM_SESSION_ID", ""),
                    "--tmux-pane",
                    os.environ.get("TMUX_PANE", ""),
                    "--config-dir",
                    claim.target,
                    "--source-dir",
                    accounts.env_config_dir(),
                    "--cwd",
                    str(payload.get("cwd") or claim.cwd or ""),
                ]
                pid = os.environ.get("CLAUDE_PID", "").strip()
                if pid.isdigit():
                    args += ["--pid", pid]
                if claim.force:
                    args.append("--force")
                if claim.no_codex:
                    args.append("--no-codex")
                spawn.spawn_ccc(args)
    return 0


def _log_event(sid: str, event: str, detail: str) -> None:
    """Append one line to the plain-text event log (pre-compact, subagent-stop, …).

    A flat tab-separated file (``app_home()/events.log``) rather than a store
    table: these are rare, append-only observations that only need `grep`.
    """
    try:
        path = config.app_home() / "events.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}\t{sid}\t{event}\t{detail}\n")
    except OSError:
        pass


def handle_pre_compact(payload: dict[str, Any]) -> int:
    """The session's context is about to be compacted.

    Logged so a session's history explains its "amnesia" moments; also flags a
    summary refresh since the daemon's next summary should re-read the state.
    """
    sid = _session_id(payload)
    if not sid:
        return 0
    _log_event(sid, "pre-compact", str(payload.get("trigger") or ""))
    with Store() as store:
        ensure_current_session(store, sid, payload.get("cwd", ""))
        store.update_fields(sid, needs_summary=True)
    return 0


def handle_subagent_start(payload: dict[str, Any]) -> int:
    """A subagent of the session started — count it and record parent activity.

    The counter is the ONLY evidence ``ccc switch-account`` has of an IN-PROCESS
    Agent-tool subagent: it spawns no child ``claude`` process (so the process-tree probe
    misses it) and its launch record can land in the transcript after the switch check
    read the file. :meth:`handle_subagent_stop` decrements it; SessionStart/SessionEnd
    reset it so a crash cannot strand a phantom.
    """
    sid = _session_id(payload)
    if not sid:
        return 0
    _log_event(sid, "subagent-start", "")
    with Store() as store:
        ensure_current_session(store, sid, payload.get("cwd", ""))
        store.bump_subagents(sid, +1)
        store.update_fields(sid, last_response_at=now_ms())
    return 0


def handle_subagent_stop(payload: dict[str, Any]) -> int:
    """A subagent of the session finished — uncount it and record parent activity."""
    sid = _session_id(payload)
    if not sid:
        return 0
    _log_event(sid, "subagent-stop", "")
    with Store() as store:
        ensure_current_session(store, sid, payload.get("cwd", ""))
        # Floored at 0: a stop whose start predates the counter never drives it negative.
        store.bump_subagents(sid, -1)
        store.update_fields(sid, last_response_at=now_ms())
    return 0


def handle_session_end(payload: dict[str, Any]) -> int:
    sid = _session_id(payload)
    if not sid:
        return 0
    with Store() as store:
        store.release_all_file_locks(sid)  # never leave a closed session holding a file
        session, _ = ensure_current_session(store, sid, payload.get("cwd", ""))
        store.reset_subagents(sid)  # the process is going: no subagent of it survives
        fields: dict[str, Any] = {"last_response_at": now_ms(), "needs_summary": True}
        if (
            session.done_check_cmd
            and not session.done
            and _run_done_check(session.done_check_cmd, session.cwd)
        ):
            fields["done"] = True
        fields["status"] = (
            Status.DONE.value if fields.get("done") or session.done else Status.PARKED.value
        )
        store.update_fields(sid, **fields)
    return 0


_HANDLERS = {
    "session-start": handle_session_start,
    "user-prompt": handle_user_prompt,
    "pre-tool-use": handle_pre_tool_use,
    "post-tool-use": handle_post_tool_use,
    "stop": handle_stop,
    "release-locks": handle_release_locks,
    "session-end": handle_session_end,
    "pre-compact": handle_pre_compact,
    "subagent-start": handle_subagent_start,
    "subagent-stop": handle_subagent_stop,
}


def _is_headless() -> bool:
    """True when this hook fired under a headless / SDK entrypoint (``claude -p``).

    Every ``claude -p`` run (``ai.py``'s commit-message generation, the daemon's own
    summary calls, any other tool shelling out to Claude) inherits the launching
    interactive session's environment — crucially ``CLAUDE_SESSION_AIM`` — and fires
    the full hook set, which would otherwise leak a junk session row stamped with the
    *parent's* AIM (the snezana-style "duplicates"). Claude sets
    ``CLAUDE_CODE_ENTRYPOINT`` to ``sdk-cli`` / ``sdk-ts`` / ``sdk-py`` for these;
    ``cli`` (or unset, on older builds) means a real user session. Mirror the
    adapter's ``entrypoint.startswith("sdk")`` filter so neither row-creating path
    — live ``discover()`` nor the hooks — ever tracks a headless run.
    """
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli").startswith("sdk")


def dispatch(event: str) -> int:
    """Entry point for ``ccc hook <event>``; never raises."""
    if _is_headless():
        return 0
    handler = _HANDLERS.get(event)
    if handler is None:
        return 0
    try:
        payload = _read_payload()
        return handler(payload)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return 0
