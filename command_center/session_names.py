#!/usr/bin/env python3
"""Hand-set session names (``ccc name``) and ccc's tab-title bookkeeping.

ccc generates no session names itself (no LLM name, no deterministic fallback; the
automatic namer and its ``session_names`` key are gone, tp#1808). What stays:

* **``ccc name -s ID "x y"``** stores a name the user (or an agent on its behalf, e.g. a
  staff agent's ``tab_name``) chose — origin ``manual``. A name another ACTIVE session
  (not done, not archived) already holds is refused (:class:`NameTaken`), casefolded,
  inside one ``BEGIN IMMEDIATE`` transaction. Running Claude processes are never renamed
  by ccc — the command prints ``/rename <name>`` for the user to type; ccc launches pass
  it as ``claude --name`` (:mod:`command_center.launch_argv`).
* **``ccc name -s ID --reset``** drops a hand-set name (``manual``, or ``manual-tab`` from
  an older ccc that adopted hand-typed tab titles) and unfreezes the tab title.
* **Tab-title bookkeeping** for :mod:`command_center.tabsymbol`: what ccc last wrote
  (:func:`record_title_write`) and whether a tab is frozen (:func:`titles_frozen`).
* **Peer names that follow the AIM** (opt-in ``peer_name_from_aim``, tp#1989): the name
  the ``UserPromptSubmit`` hook asks Claude Code for as ``sessionTitle``
  (:func:`peer_name`, :func:`dedupe_peer_name`). ccc still writes no registry file.

Import-light: the store and config layers are imported lazily where used.
"""

# pylint: disable=import-outside-toplevel

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import re
import sqlite3
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from .models import LiveSession, Session
    from .store import Store

ORIGIN_MANUAL = "manual"
ORIGIN_MANUAL_TAB = "manual-tab"
# Origins the user authored; only these are dropped by ``ccc name --reset``.
MANUAL_ORIGINS = frozenset({ORIGIN_MANUAL, ORIGIN_MANUAL_TAB})

# A name the user types (``ccc name``) stays a name, not a sentence.
MAX_MANUAL_CHARS = 48

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WS_RE = re.compile(r"\s+")


class NameTaken(Exception):
    """``ccc name`` asked for a name another active session already holds."""

    def __init__(self, name: str, holder: str) -> None:
        super().__init__(f"name {name!r} is already used by session {holder[:8]}")
        self.name = name
        self.holder = holder


def clean_manual(name: str) -> str:
    """A user-typed name (``ccc name``): control characters removed, whitespace collapsed."""
    return _WS_RE.sub(" ", _CONTROL_RE.sub("", name or "")).strip()


def _key(name: str) -> str:
    return name.casefold()


def _taken(conn: sqlite3.Connection, exclude: str) -> dict[str, str]:
    """``{casefolded name: session id}`` of every ACTIVE named row except *exclude*."""
    rows = conn.execute(
        "SELECT session_id, canonical_name FROM sessions "
        "WHERE canonical_name IS NOT NULL AND canonical_name != '' "
        "AND done = 0 AND archived = 0 AND session_id != ?",
        (exclude,),
    ).fetchall()
    return {_key(str(r[1])): str(r[0]) for r in rows}


def rename(store: Store, session_id: str, name: str) -> str:
    """``ccc name``: set *name* (origin ``manual``). Raises :class:`NameTaken` / ``ValueError``.

    Clears ``title_written`` so ccc's next title write lands unconditionally — the user
    asked for this name, so it may replace even a hand-set tab title.
    """
    cleaned = clean_manual(name)
    if not cleaned:
        raise ValueError("a name needs at least one visible character")
    if len(cleaned) > MAX_MANUAL_CHARS:
        raise ValueError(f"a name is at most {MAX_MANUAL_CHARS} characters")
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"no session {session_id}")
        taken = _taken(conn, session_id)
        if _key(cleaned) in taken:
            raise NameTaken(cleaned, taken[_key(cleaned)])
        conn.execute(
            "UPDATE sessions SET canonical_name = ?, canonical_name_origin = ?, "
            "name_source_aim = NULL, title_written = NULL, updated_at = ? "
            "WHERE session_id = ?",
            (cleaned, ORIGIN_MANUAL, int(time.time() * 1000), session_id),
        )
        conn.commit()
        return cleaned
    except BaseException:
        conn.rollback()
        raise


def reset_name(store: Store, session_id: str) -> str | None:
    """``ccc name --reset``: drop a hand-set name and unfreeze the session's tab title.

    Only a ``manual`` / ``manual-tab`` name is dropped (``None`` is returned); any other
    stored name (from an older ccc's automatic namer) is returned as it is. ``title_written``
    is cleared so the next title sync writes the tab unconditionally. Raises
    ``ValueError`` for an unknown session.
    """
    session = store.get(session_id)
    if session is None:
        raise ValueError(f"no session {session_id}")
    if session.canonical_name_origin not in MANUAL_ORIGINS:
        return session.canonical_name or None
    store.conn.execute(
        "UPDATE sessions SET canonical_name = NULL, canonical_name_origin = '', "
        "name_source_aim = NULL, name_upgrade_tries = 0, title_written = NULL, "
        "updated_at = ? WHERE session_id = ? AND canonical_name_origin = ?",
        (int(time.time() * 1000), session_id, session.canonical_name_origin),
    )
    store.conn.commit()
    return None


# --------------------------------------------------------------------------- #
# Peer names that follow the AIM (tp#1989, opt-in ``peer_name_from_aim``)
# --------------------------------------------------------------------------- #
#: Longest peer name ccc asks Claude Code for. Claude Code applies a hook ``sessionTitle``
#: verbatim (no clipping, no dedupe), so ccc clips and dedupes itself.
PEER_NAME_MAX = 40

#: What a clipped name must not end in (a cut ``fix decide: x`` reads ``fix decide``).
_PEER_TRAIL = " \t:;,.—–-·"


def aim_peer_words(aim: str | None) -> str:
    """The naming rule: the words of *aim* a peer name is made of (``""`` when none).

    The AIM's first line without its trailing ``: tp#N, #M`` run (:func:`aimtext.words`),
    words as typed (spaces, ``:`` and ``#`` survive). The ONE place the rule lives, so a
    different rule (e.g. an AIM improver) replaces this function alone.
    """
    from . import aimtext

    return aimtext.words(aim)


def clip_words(text: str, width: int) -> str:
    """*text* cut to at most *width* chars at a word boundary (a single long word is cut)."""
    if len(text) <= width:
        return text
    cut = text[: width + 1].rfind(" ")
    head = text[:cut] if cut > 0 else text[:width]
    return head.rstrip(_PEER_TRAIL) or text[:width].rstrip()


def peer_name(session: Session | None, user_aim: str | None) -> str | None:
    """The peer name *session* should carry, or ``None`` (Claude Code keeps its own name).

    A hand-set ``canonical_name`` (origin ``manual``) wins; else the naming rule
    (:func:`aim_peer_words`) over *user_aim* (the hand-set AIM, :meth:`Store.hand_set_aims`),
    control characters removed, whitespace collapsed, clipped at a word boundary to
    :data:`PEER_NAME_MAX`. Empty → ``None``.
    """
    if session is not None and session.canonical_name_origin == ORIGIN_MANUAL:
        manual = clean_manual(session.canonical_name or "")
        if manual:
            return clip_words(manual, PEER_NAME_MAX)
    # Whitespace (a tab too) becomes one space BEFORE the control characters go.
    spaced = _WS_RE.sub(" ", aim_peer_words(user_aim))
    text = _WS_RE.sub(" ", _CONTROL_RE.sub("", spaced)).strip()
    if not text:
        return None
    return clip_words(text, PEER_NAME_MAX) or None


def dedupe_peer_name(name: str, taken: set[str]) -> str:
    """*name*, or ``<name> 2``, ``<name> 3`` … — the first one no other live peer holds.

    *taken* holds the other live sessions' peer names, casefolded. The base is re-clipped
    so a suffixed name still fits :data:`PEER_NAME_MAX`.
    """
    if _key(name) not in taken:
        return name
    for n in range(2, 100):
        suffix = f" {n}"
        candidate = clip_words(name, PEER_NAME_MAX - len(suffix)) + suffix
        if _key(candidate) not in taken:
            return candidate
    return name


def hook_title(
    store: Store,
    session_id: str,
    current: str | None,
    live_sessions: Callable[[], Iterable[LiveSession]],
) -> str | None:
    """The ``sessionTitle`` a ``UserPromptSubmit`` hook asks for, or ``None``.

    *current* is the hook input's ``session_title`` (``None`` when absent: the registry
    name is compared instead). Nothing when the name equals it — checked before the
    registry is read, so a settled session pays one small query. *live_sessions* is read
    only then: the session must be a registered interactive one, and the name is deduped
    against every OTHER live session's peer name.
    """
    hand = store.hand_set_aims([session_id]).get(session_id)
    base = peer_name(store.get(session_id), hand.text if hand else None)
    if not base or base == current:
        return None
    lives = list(live_sessions())
    own = next((ls for ls in lives if ls.session_id == session_id), None)
    if own is None or own.kind != "interactive":
        return None
    taken = {
        _key(name)
        for ls in lives
        if ls.session_id != session_id and ls.alive and (name := (ls.name or "").strip())
    }
    wanted = dedupe_peer_name(base, taken)
    held = current if current is not None else (own.name or "").strip()
    return None if wanted == held else wanted


# --------------------------------------------------------------------------- #
# Tab-title bookkeeping
# --------------------------------------------------------------------------- #
def record_title_write(store: Store, session_id: str, core: str, now_ms: int | None = None) -> None:
    """Remember that ccc just wrote *core* as this session's tab title (generation += 1)."""
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    store.conn.execute(
        "UPDATE sessions SET title_written = ?, title_generation = title_generation + 1, "
        "title_written_at = ? WHERE session_id = ?",
        (core, stamp, session_id),
    )
    store.conn.commit()


def titles_frozen(session: Session) -> bool:
    """True when ccc must never write this session's tab title (an older ccc adopted a
    title the user typed by hand; ``ccc name --reset`` unfreezes it)."""
    return session.canonical_name_origin == ORIGIN_MANUAL_TAB
