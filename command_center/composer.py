#!/usr/bin/env python3
"""Classify Claude Code's input box from a :class:`~command_center.screenread.ScreenRead` (tp#1844).

The box is the lines strictly between the last two full-width ``─`` rules on the screen
(Claude Code 2.1.319: no rounded frame; the status line and footer hints lie below the bottom
rule, queued messages and menus above the top rule). Its first line opens with a prompt glyph:
``❯`` (``>`` on older builds) in prompt mode, ``!`` in bash mode, ``#`` in memory mode (not in
2.1.319, where ``#`` is plain text).

``chars`` counts the cells inside the box after the glyph that are neither blank nor dim. Dim
is the cell's ``faint`` flag: the placeholder and queued-turn hint text are faint in the
default colour, so a grey foreground alone is not dim (fixtures in ``tests/fixtures/composer``).
One exception: Ink draws its cursor INVERSE, possibly on the first letter of a dim placeholder
or suggestion, so the cell at the cursor is ignored when it is inverse and every later cell on
that line is dim or blank.

:func:`read_composer` is the pure parser; :func:`parse_screen` wraps it for callers that must
never fail (an unreachable screen or a parser exception becomes ``unreadable``). The draft text
never leaves this module: a :class:`Composer` carries a count, a mode and, on a failure, only
the exception's TYPE name.

Stdlib only.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from command_center.screenread import ScreenCell, ScreenRead, rule_rows

#: ``empty`` / ``text`` / ``unrecognized`` come from :func:`read_composer`; ``unreadable``
#: only from :func:`parse_screen` (no screen, or the parser raised).
ComposerState = Literal["empty", "text", "unrecognized", "unreadable"]
ComposerMode = Literal["prompt", "bash", "memory", "unknown"]

#: The glyph opening the box's first line → the input mode it stands for.
MODE_GLYPHS: dict[str, ComposerMode] = {"❯": "prompt", ">": "prompt", "!": "bash", "#": "memory"}


@dataclass(frozen=True)
class Composer:
    """The input box's classification: never its text.

    ``chars`` = content cells counted (a lower bound when the box's top is off the screen);
    ``mode`` = the glyph's mode (``unknown`` when no glyph was read); ``error`` = the exception
    type name when :func:`parse_screen` caught one, else ``""``.
    """

    state: ComposerState
    chars: int = 0
    mode: ComposerMode = "unknown"
    error: str = ""


def is_blank(cell: ScreenCell) -> bool:
    """True for an untouched or wide-char tail cell, whitespace (U+00A0 included) or NUL."""
    return all(c.isspace() or c == "\x00" for c in cell.ch)


def is_dim(cell: ScreenCell) -> bool:
    """True when the cell is drawn faint (placeholder / suggestion text, not content)."""
    return cell.faint


def _glyph_column(cells: Sequence[ScreenCell]) -> int | None:
    """The column of the first non-blank cell when it is a known prompt glyph, else ``None``."""
    for x, cell in enumerate(cells):
        if is_blank(cell):
            continue
        return x if cell.ch in MODE_GLYPHS else None
    return None


def _count_line(cells: Sequence[ScreenCell], start: int, cursor_x: int | None) -> int:
    """Content cells of one box line from column *start* on.

    The cell at *cursor_x* is skipped when it is inverse and every later cell is dim or blank
    (Ink's cursor on the first letter of a dim placeholder).
    """
    skip = -1
    if cursor_x is not None and start <= cursor_x < len(cells) and cells[cursor_x].inverse:
        if all(is_dim(c) or is_blank(c) for c in cells[cursor_x + 1 :]):
            skip = cursor_x
    return sum(
        1
        for x in range(start, len(cells))
        if x != skip and not is_blank(cells[x]) and not is_dim(cells[x])
    )


def read_composer(read: ScreenRead) -> Composer:
    """Classify the input box on *read* (see the module docstring for the rules).

    * no full-width rule → ``unrecognized``;
    * one rule only (a draft taller than the screen pushed the top rule off) → ``text``, with
      the content cells above the rule as a lower-bound count;
    * an empty box or a first line without a known glyph → ``unrecognized``;
    * else ``text`` when a cell counts or the mode is not ``prompt``, otherwise ``empty``.
    """
    rows = rule_rows(read)
    if not rows:
        return Composer("unrecognized")
    cursor_x, cursor_y = read.cursor
    bottom = rows[-1]
    if len(rows) == 1:
        chars = sum(
            _count_line(read.lines[y], 0, cursor_x if y == cursor_y else None)
            for y in range(bottom)
        )
        return Composer("text", chars, "unknown")
    top = rows[-2]
    if bottom - top < 2:
        return Composer("unrecognized")
    first = read.lines[top + 1]
    glyph_x = _glyph_column(first)
    if glyph_x is None:
        return Composer("unrecognized")
    mode = MODE_GLYPHS[first[glyph_x].ch]
    chars = 0
    for y in range(top + 1, bottom):
        start = glyph_x + 1 if y == top + 1 else 0
        chars += _count_line(read.lines[y], start, cursor_x if y == cursor_y else None)
    state: ComposerState = "text" if chars > 0 or mode != "prompt" else "empty"
    return Composer(state, chars, mode)


def parse_screen(read: ScreenRead | None) -> Composer:
    """:func:`read_composer` that never raises: ``None`` or any exception → ``unreadable``.

    On an exception only its type name is kept (``error``): its message could quote screen
    text.
    """
    if read is None:
        return Composer("unreadable")
    try:
        return read_composer(read)
    except Exception as exc:  # pylint: disable=broad-exception-caught  # fail typed, never leak
        return Composer("unreadable", error=type(exc).__name__)
