#!/usr/bin/env python3
"""The ``ccc statusline`` rows and their pure helpers — stdlib only (tp#1806 B1).

The status line runs ``ccc statusline`` on every uncached render of every live session, so
the row rendering and every pure helper it needs live here, importable without ``models``,
``park``, ``config``, ``store`` or ``cli``. :func:`statusline_text` turns already-gathered
data into the exact text :func:`command_center.cli.cmd_statusline` prints; the clock is an
explicit ``now`` argument so two callers render byte-identical rows.

:mod:`command_center.models` re-exports the todo/AIM/drift/effort helpers,
:mod:`command_center.park` re-exports :func:`format_fire`, :func:`_span` and
:data:`FIRE_GRACE_SEC`, and :mod:`command_center.cli` the ``_aim_*`` row builders, so every
existing import keeps working and every caller runs the same code.

The helpers take any object with the :class:`~command_center.models.Session` attributes they
read (:class:`SessionView`, a type-checking-only protocol): ``cli`` passes the ``Session``.

Imports: the standard library, :mod:`command_center.bars` and :mod:`command_center.aimtext`
(both stdlib only).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import json
import re
import time
from typing import TYPE_CHECKING

from . import aimtext
from .bars import _paint_done_word, done_bar_parts, effective_progress, progress_bar

if TYPE_CHECKING:  # pragma: no cover - typing only; nothing here runs at import time
    from typing import Protocol

    class SessionView(Protocol):
        """The read-only :class:`~command_center.models.Session` attributes rendered here."""

        @property
        def aim(self) -> str | None: ...  # noqa: D102
        @property
        def short_aim(self) -> str | None: ...  # noqa: D102
        @property
        def first_aim(self) -> str | None: ...  # noqa: D102
        @property
        def first_short_aim(self) -> str | None: ...  # noqa: D102
        @property
        def aim_score(self) -> int: ...  # noqa: D102
        @property
        def aim_prev(self) -> str | None: ...  # noqa: D102
        @property
        def aim_met(self) -> bool: ...  # noqa: D102
        @property
        def manual_progress(self) -> int | None: ...  # noqa: D102
        @property
        def next_step(self) -> str | None: ...  # noqa: D102
        @property
        def drift_severity(self) -> str: ...  # noqa: D102
        @property
        def drift_reason(self) -> str | None: ...  # noqa: D102
        @property
        def drift_ack_at(self) -> int: ...  # noqa: D102
        @property
        def fire_at(self) -> int: ...  # noqa: D102
        @property
        def todos(self) -> str | None: ...  # noqa: D102


# Valid ``claude --effort`` levels. Job launches pass the config ``launch_effort``
# (default xhigh) explicitly so the session's effort never silently depends on the
# user's ~/.claude/settings.json ``effortLevel`` (whose absence means "high").
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh")

# The daemon dispatches armed jobs only this far past fire_at, so a live foreground
# waiter (which fires exactly on time) always wins the race for its own tab.
FIRE_GRACE_SEC = 120

GREEN, DIM, RESET = "\033[38;5;42m", "\033[38;5;244m", "\033[0m"
#: The colour of each status word on the ``Status:`` row (and the todo/fire chips).
STATUS_COLOR: dict[str, str] = {
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

_SL_WIDTH = 100  # status-line soft width: only the `/aim (1)` anchor row is clipped to it
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

#: The ``first_aim`` / ``first_short_aim`` select-list items of a ``FROM sessions s`` query:
#: revision (1) of the session's AIM and its short label, read straight off ``aim_history``
#: (NULL for an AIM that predates history tracking). ONE constant shared by
#: ``store._SESSION_SELECT`` and the lean status line's session read, so both derive them
#: identically.
FIRST_AIM_SELECT = """(SELECT h.aim FROM aim_history h WHERE h.session_id = s.session_id
         ORDER BY h.created_at, h.id LIMIT 1) AS first_aim,
       (SELECT h.short_aim FROM aim_history h WHERE h.session_id = s.session_id
         ORDER BY h.created_at, h.id LIMIT 1) AS first_short_aim"""


# ---- pure session helpers (re-exported by models) ---------------------------


def loads_todos(raw: str | None) -> list[tuple[str, str]]:
    """Parse a stored todo JSON string back to ``[(status, subject), …]`` (empty on junk)."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[tuple[str, str]] = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((str(item[0]), str(item[1])))
    return out


def todos_counts(todos: list[tuple[str, str]]) -> tuple[int, int]:
    """Return ``(completed, total)`` counts for a todo list."""
    return (sum(1 for status, _ in todos if status == "completed"), len(todos))


def todo_box(status: str) -> str:
    """Checkbox glyph for a todo *status*: ``☒`` done, ``◧`` in-progress, ``☐`` pending.

    Shared by the one-line status-line strip and the TUI detail list so the two
    surfaces show the same boxes.
    """
    if status == "completed":
        return "☒"
    if "progress" in status:
        return "◧"
    return "☐"


def drift_unresolved(session: SessionView) -> bool:
    """True if the session has a flagged, not-yet-acknowledged sub-goal drift (blue dot).

    A new flag resets ``drift_ack_at`` to 0; ``ack_drift`` sets it > 0; a clean check
    clears the severity. So "unresolved" = flagging severity AND unacknowledged.
    """
    return session.drift_severity in ("low", "medium", "high") and session.drift_ack_at == 0


def low_aim_score(aim: str | None, score: int, threshold: int) -> bool:
    """True when an AIM has a known concreteness score below the configured threshold."""
    return bool(aim) and 0 <= score < threshold


def aim_score_pct(aim: str | None, score: int) -> str:
    """Numeric AIM-score chip for the ``/aim`` column.

    ``""`` when no AIM is set OR while the AIM is still unscored (``score < 0``) — the
    raw ``-1`` sentinel is never surfaced. With ``aim_score_on_set`` disabled the LLM
    refine never fires, so a bare ``-1`` would otherwise read as a permanently stuck
    "pending" state; a blank chip degrades gracefully instead. Else ``"NN%"`` (0–100).
    """
    if not aim or score < 0:
        return ""
    return f"{score}%"


def display_aim(session: SessionView, first: bool = False) -> str | None:
    """The compact AIM label: the cheap-model short label if one was generated, else the
    full AIM verbatim. The full AIM is always kept intact in the store and shown in the
    detail pane / aim-history; this is what the narrow ``/aim`` column AND the in-session
    status line render. ``store.set_aim`` clears the label on every AIM change, so this is
    always the LATEST revision's label (or the full new AIM until the generator backfills).

    With *first* (config ``aim_column = "first"``, the default) it renders revision **(1)**
    instead — the done-condition as originally typed. That keeps the ``/aim`` column, and
    every "which job is this?" label built from it, stable while the AIM is sharpened over
    the session's life; the current AIM stays visible in the status line, the detail pane
    and ``ccc aim-history``. Sessions whose AIM predates history tracking have no recorded
    revision (1) — they fall back to the live AIM.
    """
    if first and session.first_aim:
        return session.first_short_aim or session.first_aim
    return session.short_aim or session.aim


def effort_from_payload(data: dict) -> str | None:
    """Best-effort extraction of a reasoning-effort level from the status-line JSON.

    Checks the top-level keys Claude Code might expose (``effort`` / ``effortLevel`` /
    ``reasoningEffort``) and, when ``model`` is a nested object, its ``effort`` /
    ``reasoning_effort`` keys. Returns a value only when it is a known
    :data:`EFFORT_LEVELS` level, else ``None`` — current Claude Code payloads carry none of
    these, so this is future-proofing that no-ops today.
    """
    candidates = [data.get("effort"), data.get("effortLevel"), data.get("reasoningEffort")]
    model = data.get("model")
    if isinstance(model, dict):
        candidates.extend([model.get("effort"), model.get("reasoning_effort")])
    for value in candidates:
        if isinstance(value, str) and value in EFFORT_LEVELS:
            return value
    return None


# ---- fire times (re-exported by park) ----------------------------------------


def _span(seconds: int) -> str:
    """Compact duration: ``45s`` / ``37m`` / ``2h 05m`` / ``2d 3h``."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, minutes = divmod(seconds // 60, 60)
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(seconds // 3600, 24)
    return f"{days}d {hours}h"


def format_fire(fire_at: int, now: int | None = None) -> str:
    """Human line for an armed fire time — overdue-aware, never a silent "now".

    ``fires 14:03 (in 37m)`` ahead of time, ``fires now`` inside the dispatch grace,
    ``overdue 37m`` past it (both dispatchers failed — the row must say so instead
    of claiming "now" forever).
    """
    now = int(time.time()) if now is None else now
    delta = int(fire_at) - now
    hhmm = time.strftime("%H:%M", time.localtime(int(fire_at)))
    if delta > 60:
        return f"fires {hhmm} (in {_span(delta)})"
    if delta >= -FIRE_GRACE_SEC:
        return "fires now"
    return f"overdue {_span(-delta)}"


# ---- status-line rows (re-exported by cli) -----------------------------------


def _render_todos_line(
    todos: list[tuple[str, str]], green: str, dim: str, reset: str, status_color: dict[str, str]
) -> str:
    """One-line TodoWrite strip: ``done/total`` counter + each item's box & short label.

    Truncates to a fixed budget so it never sprawls across the status line.
    """
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


def _aim_row_text(aim: str | None) -> str:
    """*aim* as an ``/aim`` row shows it: the words without the trailing item run (tp#1553).

    The held items already have their own status-line rows, so ``fix decide: tp#1363, #1364``
    renders as ``fix decide``. An AIM without a run is returned unchanged; one that is the
    run alone (no words) keeps its full text rather than going blank.
    """
    text = aim or ""
    parts = aimtext.split(text)
    return parts.words if parts.ids and parts.words else text


def _first_aim_anchor(  # pylint: disable=too-many-positional-arguments  # colour triple passed through
    session: SessionView, index: int, changed: bool, green: str, dim: str, reset: str
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
    first = _aim_row_text((session.first_short_aim or session.first_aim or "").strip())
    if not first:
        return []
    prefix = f"{green}{_aim_label(1)}:{reset} "
    body_w = max(24, _SL_WIDTH - _vlen(prefix))
    if len(first) > body_w:
        first = first[: body_w - 1] + "…"
    return [f"{prefix}{dim}{first}{reset}"]


def _aim_statusline_lines(  # pylint: disable=too-many-positional-arguments,too-many-arguments,too-many-locals  # colour triple passed through
    session: SessionView,
    checked: int,
    total: int,
    threshold: int,
    colors: tuple[str, str, str, str],
    index: int,
    role_marker: str = "",
    staff_name: str = "",
    *,
    show_score: bool = True,
    show_history: bool = True,
) -> list[str]:
    """The ``/aim`` status-line row(s): AIM + running index + progress bar + vague marker.

    ``role_marker`` (``🎭`` + the agent's symbol when the session adopted a staff role,
    :func:`staffrole.role_marker`) is put right after the current row's ``/aim …:`` label; the
    ``/aim (1):`` anchor row and the old side of a transition never carry it.

    ``staff_name`` (the adopted agent's ``tab_name``, :func:`staffrole.session_tab_name`)
    replaces the AIM text: a staff session is named after its agent, so its row reads
    ``/aim: 🎭<symbol> <agent name> #123, #456`` — the name first, then the ids of the AIM's
    work-item run (:mod:`.aimtext`, tp#1366).

    ``index`` is the current AIM's running number in the session's AIM history, so the
    prefix reads ``/aim (N):``. When the AIM changed this turn (``aim_prev`` set) it renders
    the transition ``/aim (N-1): <old>  ====> /aim (N): <new>`` — always on ONE row, however
    long: the status line is precious vertical space, so a wide row is left to the terminal's
    own soft wrap instead of being split into an old-AIM row, wrapped new-AIM rows and a
    trailing bar row.

    Whenever the current revision is not the first, :func:`_first_aim_anchor` prepends an
    ``/aim (1):`` row, so the originally typed done-condition stays visible for the whole
    session no matter how often the AIM is sharpened.

    The AIM text rendered is :func:`display_aim` — the latest short label when one exists,
    else the full AIM. ``store.set_aim`` clears the label on every change, so a stale label
    from a prior revision can never show here.

    ``show_score=False`` (config ``aim_score = false``) renders the AIM as unscored: no
    chip, no "vague" marker. ``show_history=False`` (config ``aim_history = false``) drops
    the this-turn transition; the caller passes ``index=0`` too, so no ``(N)`` and no anchor.
    """
    green, dim, reset, red = colors
    mark = f"{role_marker} " if role_marker else ""
    if not session.aim:
        if staff_name:
            return [f"{green}/aim:{reset} {mark}{staff_name}"]
        return [f"{green}/aim:{reset} {mark}{dim}(set one with /aim){reset}"]
    aim_score = session.aim_score if show_score else -1
    aim_prev = session.aim_prev if show_history else None
    label = _aim_label(index)
    low_score = low_aim_score(session.aim, aim_score, threshold)
    chip_color = red if low_score else dim
    score_pct = aim_score_pct(session.aim, aim_score)
    chip = f"{chip_color}{score_pct}{reset} " if score_pct else ""
    new_aim = _aim_row_text(display_aim(session))
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
    if staff_name:  # the agent's name, then the item ids: no AIM text, score or history
        ids = aimtext.ids_text(aimtext.split(session.aim).ids)
        body = f"{staff_name} {ids}" if ids else staff_name
        return [f"{green}{label}:{reset} {mark}{body}{prog}"]
    changed = bool(aim_prev) and aim_prev != session.aim
    anchor = _first_aim_anchor(session, index, changed, green, dim, reset)
    if not changed:
        return [*anchor, f"{green}{label}:{reset} {mark}{chip}{new_aim}{prog}{tag}"]
    bold = "\033[1m"
    old_part = f"{green}{_aim_label(index - 1)}:{reset} {dim}{_aim_row_text(aim_prev)}{reset}"
    arrow = f"{bold}====>{reset} {green}{label}:{reset} {mark}"
    # Always ONE line — old AIM, arrow, new AIM, bar and marker never split across rows;
    # an over-wide line soft-wraps in the terminal rather than costing three status-line rows.
    return [*anchor, f"{old_part}   {arrow}{chip}{new_aim}{prog}{tag}"]


def statusline_text(  # pylint: disable=too-many-arguments,too-many-locals  # one row per argument group
    session: SessionView | None,
    *,
    status_value: str,
    checked: int,
    total: int,
    aim_index: int,
    armed: tuple[int, int] | None,
    role_marker: str,
    staff_name: str,
    threshold: int,
    aim_score: bool,
    aim_history: bool,
    now: int,
) -> str:
    """Every ``ccc statusline`` row for already-gathered data, as one newline-terminated text.

    ``session`` is ``None`` for an untracked session (no store row): the ``/aim`` row (staff
    name or the "set one" hint) and ``Status: untracked``. Otherwise ``status_value`` is the
    live status (or the stored one), ``checked``/``total`` the checklist counts, ``aim_index``
    the current AIM's running number, ``armed`` the store's ``(earliest fire_at, count)`` of
    armed drafts (or ``None``); ``threshold``/``aim_score``/``aim_history`` come from the
    config. ``now`` (epoch seconds) is the one clock every fire time is formatted against.
    """
    green, dim, reset = GREEN, DIM, RESET
    status_color = STATUS_COLOR
    if session is None:
        mark = f"{role_marker} " if role_marker else ""
        if staff_name:
            first = f"{green}/aim:{reset} {mark}{staff_name}"
        else:
            first = f"{green}/aim:{reset} {mark}{dim}(set one with /aim){reset}"
        return f"{first}\n{dim}Status: untracked   /next-step: —{reset}\n"
    if not aim_history:  # plain "/aim: <current>": no (N), no anchor row, no transition
        aim_index = 0
    lines = _aim_statusline_lines(
        session,
        checked,
        total,
        threshold,
        (green, dim, reset, status_color["failed"]),
        aim_index,
        role_marker,
        staff_name,
        show_score=aim_score,
        show_history=aim_history,
    )
    scolor = status_color.get(status_value, dim)
    nxt = session.next_step.splitlines()[0] if session.next_step else "—"
    lines.append(f"Status: {scolor}{status_value}{reset}   /next-step: {nxt}")

    # Impartial-checker drift warning (the blue dot, in every session's status line).
    if drift_unresolved(session):
        blue = "\033[38;5;39m"
        why = session.drift_reason or "see ccc subgoal-history"
        lines.append(
            f"{blue}● sub-goal drift ({session.drift_severity}){reset}: {why}  (ccc ack-drift)"
        )

    # Live TodoWrite list as a single line: left counter + each item's box & short label.
    todos = loads_todos(session.todos)
    if todos:
        lines.append(_render_todos_line(todos, green, dim, reset, status_color))

    # Armed parked prompt(s): one chip in EVERY session's statusline — the soonest fire
    # time, overdue-aware (red once both dispatchers should have fired; see park.py).
    # A prompt attached to THIS session gets its own explicit line first.
    if session.fire_at:
        own = format_fire(session.fire_at, now)
        own_color = status_color["failed"] if own.startswith("overdue") else "\033[38;5;214m"
        lines.append(f"{own_color}⏳ parked prompt for THIS session {own}{reset}")
    if armed:
        earliest, count = armed
        fire_text = format_fire(earliest, now)
        more = f"  +{count - 1} more" if count > 1 else ""
        chip_color = status_color["failed"] if fire_text.startswith("overdue") else dim
        lines.append(f"{chip_color}⏳ parked prompt {fire_text}{more}  (ccc jobs){reset}")
    return "".join(f"{line}\n" for line in lines)
