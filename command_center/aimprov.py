#!/usr/bin/env python3
"""A session's hand-set AIM: the one naming rule over its ``aim_history`` rows (tp#1991).

The AIM a person set for a session, read from the rows' ``set_by`` stamps (the rule
decided for tp#1986, used by ``ccc sessions -j`` ``user_aim``, ``ccc lineage -j``
``data.thread.user_aim`` and the peer name):

1. the newest row stamped ``user`` or ``launch`` (``created_at``, then ``id``, newest
   wins) → tier ``user``;
2. else the newest row stamped ``relay`` or unstamped (NULL) → tier ``unstamped``;
3. else — no rows at all — a non-blank current AIM (a pre-history session) → that text,
   ``by`` and ``at`` unknown, tier ``unstamped``;
4. else ``None``: a ``tool`` row never answers, however new.

:func:`stamp_row` is the D2 match rule both :meth:`Store.stamp_of` and ``ccc
restamp-aims`` use: which of a session's rows a carried AIM text came from.

A relay successor's carried anchor (its FIRST row, a copy of the source's first row that
``Store.carry_first_aim`` backdates) is skipped: it would otherwise let the root's first
AIM beat the successor's own newer rows. :meth:`Store.latest_user_aim` stays the HANDOVER
rule (what a successor starts with); this module is the naming rule.

Pure: no I/O, no store import.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position  # the direct-run shim comes first
from collections.abc import Iterable, Sequence
from typing import NamedTuple

from . import aimtext
from .models import AIM_SET_BY_LAUNCH, AIM_SET_BY_RELAY, AIM_SET_BY_TOOL, AIM_SET_BY_USER

__all__ = ["TIER_UNSTAMPED", "TIER_USER", "AimRow", "HandAim", "hand_set_aim", "stamp_row"]

#: The tier of a row a person set (``user``, ``launch``).
TIER_USER = "user"
#: The tier of a row whose author is unknown (``relay``, NULL, a pre-history AIM).
TIER_UNSTAMPED = "unstamped"

_USER_STAMPS = frozenset({AIM_SET_BY_USER, AIM_SET_BY_LAUNCH})

#: One ``aim_history`` row: ``(aim, created_at, set_by, id)``.
AimRow = tuple[str, int, str | None, int]


class HandAim(NamedTuple):
    """A session's hand-set AIM (:func:`hand_set_aim`)."""

    text: str  # the full row text
    by: str | None  # user / launch / relay / None (unstamped or pre-history)
    at_ms: int | None  # epoch ms the row was written; None for a pre-history AIM
    tier: str  # TIER_USER / TIER_UNSTAMPED


def hand_set_aim(
    rows: Iterable[AimRow], current_aim: str | None, anchor: str | None = None
) -> HandAim | None:
    """*rows*' hand-set AIM by the module rule, or ``None``.

    *rows* in any order; *current_aim* the session's ``sessions.aim``; *anchor* the text
    of the source's FIRST row when the session is a relay successor (``handed_off_from``):
    the session's own first row is skipped when it holds that text and later rows exist.
    Rows with a blank text never answer.
    """
    ordered = sorted(rows, key=lambda r: (int(r[1] or 0), int(r[3] or 0)))
    if not ordered:
        text = (current_aim or "").strip()
        return HandAim(text, None, None, TIER_UNSTAMPED) if text else None
    if anchor is not None and len(ordered) > 1 and ordered[0][0] == anchor:
        ordered = ordered[1:]
    candidates = [r for r in reversed(ordered) if str(r[0] or "").strip()]
    for stamps, tier in ((_USER_STAMPS, TIER_USER), (None, TIER_UNSTAMPED)):
        for aim, at, by, _id in candidates:
            hit = by in stamps if stamps is not None else (by is None or by == AIM_SET_BY_RELAY)
            if hit:
                return HandAim(aim, by, int(at or 0) or None, tier)
    return None


def stamp_row(aims: Sequence[str], stamps: Sequence[str | None], text: str) -> int | None:
    """The index of the row whose stamp a carried *text* takes, or ``None`` (no match).

    *aims* / *stamps* are one session's rows oldest first (``created_at``, then ``id``).
    The match is the newest row holding *text* exactly, else the newest equal to it after
    :func:`aimtext.words` on both sides. A ``tool`` row that only stripped the trailing id
    run off the row before it (``ccc strip-aim-runs``) is looked through: the words are
    that earlier row's, so the earlier row's index is answered (tp#1991).
    """
    match = next((i for i in reversed(range(len(aims))) if aims[i] == text), None)
    key = aimtext.words(text).strip()
    if match is None and key:
        match = next(
            (i for i in reversed(range(len(aims))) if aimtext.words(aims[i]).strip() == key),
            None,
        )
    while match is not None and match > 0:
        rewrite = (
            stamps[match] == AIM_SET_BY_TOOL
            and aims[match - 1] != aims[match]
            and aimtext.strip_tail_run(aims[match - 1]) == aims[match]
        )
        if not rewrite:
            break
        match -= 1
    return match
