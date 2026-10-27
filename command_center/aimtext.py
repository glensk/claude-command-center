#!/usr/bin/env python3
"""An AIM's words and its work-item ids: ``fix decide: tp#1363, #1364`` (tp#1366).

A session's AIM may end in the run of work items it holds — ``<few words>: tp#N, #M``, the
form an external tracker keeps up to date with ``ccc set-aim`` — or, in the older form,
lead with it (``tp#N, #M: <few words>``). This module splits the two parts so a tab
title can show the words alone, a staff session's ``/aim`` row the ids alone, and the
status line can clip the words without cutting the ids off.

Pure string functions, no imports beyond ``re``: they run on every status-line render.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import re
from typing import NamedTuple

#: One run of item references: ``tp#7``, ``tp#7, #8``, ``tp 7`` (the ``#`` is optional).
_RUN = r"tp(?:\s*#\s*|\s+)\d+(?:(?:,\s?|[,\s]+(?:and\s+)?#)\d+)*"
#: The trailing run: a colon, then one run that reaches the end of the text.
_TAIL_RE = re.compile(rf":\s*({_RUN})\s*$")
#: The leading run of the older form, followed by a word boundary.
_LEAD_RE = re.compile(rf"^({_RUN})\b")
_ID_RE = re.compile(r"\d+")
#: What separates the leading run from the words (``tp#7: words``, ``tp#7 — words``).
_SEPARATORS = " \t:—–-·"


class AimParts(NamedTuple):
    """The words of an AIM and the ids of its item run (``()`` when it has none)."""

    words: str
    ids: tuple[int, ...]


def split(aim: str | None) -> AimParts:
    """*aim*'s first line split into its words and its item ids.

    The trailing ``…: tp#A, #B`` run wins; else a leading ``tp#A, #B`` run (the older
    form); else the whole line is words. Duplicate ids keep their first position.
    """
    text = (aim or "").strip()
    line = text.splitlines()[0].strip() if text else ""
    tail = _TAIL_RE.search(line)
    if tail is not None:
        head = line[: tail.start()].rstrip(_SEPARATORS)
        run = tail.group(1)
    else:
        lead = _LEAD_RE.match(line)
        if lead is None:
            return AimParts(line, ())
        head = line[lead.end() :].lstrip(_SEPARATORS)
        run = lead.group(1)
    ids = tuple(dict.fromkeys(int(text) for text in _ID_RE.findall(run)))
    return AimParts(head, ids)


def words(aim: str | None) -> str:
    """The AIM's words without its item run (``fix decide: tp#1363`` → ``fix decide``)."""
    return split(aim).words


def title_words(aim: str | None, width: int) -> str:
    """The AIM words a tab title shows, cut to *width* chars (``""``: the AIM is ids only)."""
    text = words(aim)
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def ids_text(ids: tuple[int, ...]) -> str:
    """``#1236, #1177`` — the short id list a staff session's ``/aim`` row shows."""
    return ", ".join(f"#{tp_id}" for tp_id in ids)


def clip(aim: str | None, width: int) -> str:
    """*aim*'s first line cut to *width* chars, the trailing item run kept whole.

    The words are clipped (``…``) so ``<words…>: tp#N`` still fits; an AIM without a
    trailing run, or one whose run alone is too wide, is cut at the end as before.
    """
    line = (aim or "").splitlines()[0] if aim else ""
    if len(line) <= width:
        return line
    tail = _TAIL_RE.search(line)
    if tail is not None:
        suffix = ": " + tail.group(1)
        room = width - len(suffix) - 1
        if room >= 8:
            head = line[: tail.start()].rstrip(_SEPARATORS)
            return head[:room].rstrip() + "…" + suffix
    return line[: width - 1] + "…"
