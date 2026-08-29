#!/usr/bin/env python3
"""Job dependencies: a job can declare it depends on another job finishing first.

The single source of truth for the ``sessions.depends_on`` feature — the state machine
(:func:`dependency_state`), the cycle guard (:func:`would_create_cycle`), reference
resolution (:func:`resolve_dependency_ref`) and the launch blocker
(:func:`launch_blocker`). Kept dependency-light (models + store types only) so the
display (``core``/``tui``/``ls``), the launch guards (``cli``/``futuresync``) and the
edit-form picker all read the SAME logic — a dependency is never classified twice.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .future_files import display_hash
from .models import Session

if TYPE_CHECKING:
    from .store import Store

# Dependency states — a job's view of the job it depends on. Plain string constants
# (matching the repo's other "small enum" style, e.g. drift_severity) so they round-trip
# through Row.dep_state and the ls/mirror labels without a Literal import at the call site.
SATISFIED = "satisfied"  # the dependency really completed (done, not cancelled)
UNMET = "unmet"  # exists but has not completed yet (running / parked / future)
CANCELLED = "cancelled"  # cancelled or trashed: archived, no real completion
MISSING = "missing"  # no row for the referenced UUID (dangling reference)

#: States that mean "the dependency is NOT satisfied" — the red marker shows for these.
UNSATISFIED = frozenset({UNMET, CANCELLED, MISSING})


def is_unsatisfied(state: str) -> bool:
    """Whether *state* means the dependency is unsatisfied (drives the red marker)."""
    return state in UNSATISFIED


def dependency_state(parent: Session | None) -> str:
    """Classify a job's dependency by the state of the job it depends on (*parent*).

    * ``missing`` — no row for the referenced UUID (dangling reference).
    * ``cancelled`` — the parent was cancelled or trashed: ``archived=1`` without a real
      completion (``ccc mark-done`` on a draft, ``ccc delete-job``). Checked BEFORE
      ``done`` because the cancel path leaves a draft carrying BOTH ``archived=1`` and
      ``done=1``.
    * ``satisfied`` — a real completion only: ``done=1`` and not the cancelled case.
    * ``unmet`` — everything else (still running / parked / a not-yet-done future job).

    ``aim_met`` (the impartial "looks done" verdict) NEVER counts — only the human
    ``done`` flag does. Display, launch guard and picker all read this ONE helper.
    """
    if parent is None:
        return MISSING
    if parent.archived:
        return CANCELLED
    if parent.done:
        return SATISFIED
    return UNMET


def would_create_cycle(
    get_session: Callable[[str], Session | None], session_id: str, depends_on: str
) -> bool:
    """Whether making *session_id* depend on *depends_on* would form a cycle.

    Walks the ``depends_on`` chain starting at *depends_on* with a visited set; returns
    ``True`` if it reaches *session_id* (a direct or indirect cycle, self-dependency
    included) or revisits any node (a pre-existing loop). A dangling reference (no row for
    a link in the chain) ends the walk cleanly with ``False``. Enforced at every write
    boundary (TUI commit, ``cmd_new_job``, futuresync import).
    """
    current = (depends_on or "").strip()
    visited: set[str] = set()
    while current:
        if current == session_id:
            return True
        if current in visited:
            return True
        visited.add(current)
        parent = get_session(current)
        if parent is None:
            return False
        current = (parent.depends_on or "").strip()
    return False


class DependencyError(ValueError):
    """A dependency reference could not be resolved (unknown or ambiguous)."""


def resolve_dependency_ref(store: Store, ref: str) -> str:
    """Resolve *ref* (a full session UUID or a unique id prefix) to a full session UUID.

    The prefix form covers the 4-hex display hash shown in the TUI / ``ccc ls`` id column
    (a UUID's string form starts with its ``.hex`` prefix). Raises :class:`DependencyError`
    when *ref* is empty, matches no session, or is an ambiguous prefix (several sessions
    share it).
    """
    needle = (ref or "").strip()
    if not needle:
        raise DependencyError("empty dependency reference")
    if store.get(needle) is not None:
        return needle
    matches = [
        s.session_id
        for s in store.list_sessions(include_archived=True)
        if s.session_id.startswith(needle)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise DependencyError(f"no session matches dependency reference {ref!r}")
    raise DependencyError(f"dependency reference {ref!r} is ambiguous ({len(matches)} sessions)")


@dataclass
class DependencyBlock:
    """A structured "this job is blocked by its dependency" record (launch guard)."""

    state: str  # UNMET / CANCELLED / MISSING (never SATISFIED — that is not a block)
    parent_id: str  # the dependency's full session UUID
    parent_hash: str  # its 4-hex display hash
    parent_aim: str  # its AIM (best-effort — "" when the parent row is missing)


def launch_blocker(store: Store, session: Session) -> DependencyBlock | None:
    """The dependency blocking *session*'s launch, or ``None`` when clear to launch.

    ``None`` when the job has no dependency or the dependency is ``satisfied``; otherwise a
    structured block the callers render (``cmd_start_job``, ``tui._start_job``,
    ``futuresync._consume_launch``). The ONE shared launch gate.
    """
    dep = (session.depends_on or "").strip()
    if not dep:
        return None
    parent = store.get(dep)
    state = dependency_state(parent)
    if state == SATISFIED:
        return None
    return DependencyBlock(
        state=state,
        parent_id=dep,
        parent_hash=display_hash(dep),
        parent_aim=(parent.aim if parent and parent.aim else "") or "",
    )
