#!/usr/bin/env python3
"""A read-only snapshot of a terminal screen's bottom lines, cell by cell (tp#1844).

:func:`command_center.terminal.iterm_screen_tail` fills a :class:`ScreenRead` from the iTerm2
Python API; ``tools/composer_probe.py`` writes one to a JSON fixture (masked, see
:func:`mask_char`) and the input-box parser reads it. Cells are per terminal COLUMN (built
with ``LineContents.string_at(x)`` / ``style_at(x)``), never by indexing the line's string,
whose character offsets drift from columns on wide characters and emoji.

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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: Glyphs that draw the input box's frame; never masked.
RULE_GLYPHS = frozenset("─│╭╮╰╯")
#: Prompt / input-mode glyphs that open the box's first line; never masked.
PROMPT_GLYPHS = frozenset("❯>!#")
#: The box's horizontal rule glyph.
RULE = "─"


@dataclass(frozen=True)
class ScreenCell:
    """One terminal cell: its text (``""`` for an untouched or wide-char tail cell) and style.

    ``fg`` is a descriptor of the foreground colour: ``""`` (none set), ``"std:N"`` (a
    256-palette index), ``"rgb:rrggbb"`` or ``"alt:NAME"`` (iTerm's alternate colours).
    """

    ch: str
    faint: bool = False
    inverse: bool = False
    fg: str = ""


@dataclass(frozen=True)
class ScreenRead:
    """The bottom lines of a screen: ``cols`` wide, ``cursor`` = ``(x, y)`` into ``lines``.

    ``y == -1`` when the cursor lies above the lines kept.
    """

    cols: int
    cursor: tuple[int, int]
    lines: tuple[tuple[ScreenCell, ...], ...]


def line_text(cells: Sequence[ScreenCell]) -> str:
    """The cells' text joined (untouched cells read as ``""``)."""
    return "".join(c.ch for c in cells)


def is_full_rule(cells: Sequence[ScreenCell], cols: int) -> bool:
    """True when the line is only ``─`` (blanks allowed around it) at least ``cols - 2`` wide."""
    text = line_text(cells).strip()
    return bool(text) and set(text) == {RULE} and len(text) >= cols - 2


def rule_rows(read: ScreenRead) -> list[int]:
    """Indices of the full-width rule lines, top to bottom."""
    return [i for i, cells in enumerate(read.lines) if is_full_rule(cells, read.cols)]


#: The ASCII prompt/mode glyphs are kept only in the first columns of a line (where the box
#: draws its glyph); elsewhere they are ordinary punctuation and masked.
GLYPH_COLUMNS = 2


def mask_char(ch: str, x: int = 0) -> str:
    """Mask one cell's text: rule/glyph cells and blanks stay, every other character goes.

    Letters become ``a``, digits ``9``, anything else (punctuation, symbols, emoji) ``x``;
    a multi-code-point cell masks each code point. ``❯`` and the frame glyphs stay in any
    column, ``> ! #`` only in column ``x < GLYPH_COLUMNS``.
    """
    if ch in RULE_GLYPHS or ch == "❯" or (ch in PROMPT_GLYPHS and x < GLYPH_COLUMNS):
        return ch
    out = []
    for c in ch:
        if c.isspace() or c == "\x00":  # iTerm reports a cell Ink skipped over as NUL
            out.append(c)
        elif c.isalpha():
            out.append("a")
        elif c.isdigit():
            out.append("9")
        else:
            out.append("x")
    return "".join(out)


def masked(read: ScreenRead) -> ScreenRead:
    """*read* with every cell's text masked (styles and geometry kept)."""
    lines = tuple(
        tuple(ScreenCell(mask_char(c.ch, x), c.faint, c.inverse, c.fg) for x, c in enumerate(cells))
        for cells in read.lines
    )
    return ScreenRead(read.cols, read.cursor, lines)


def tail_from_top_rule(read: ScreenRead, above: int, fallback: int = 20) -> ScreenRead:
    """Keep the lines from *above* rows over the top box rule downward.

    The top rule is the second-to-last full-width rule; without such a pair only the bottom
    *fallback* lines are kept. The cursor's ``y`` is re-based (``-1`` when cut off).
    """
    rows = rule_rows(read)
    if len(rows) >= 2:
        start = max(0, rows[-2] - max(0, above))
    else:
        start = max(0, len(read.lines) - fallback)
    x, y = read.cursor
    new_y = y - start if 0 <= y - start < len(read.lines) - start else -1
    return ScreenRead(read.cols, (x, new_y), read.lines[start:])


def _runs(values: Iterable[str]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for v in values:
        if out and out[-1][0] == v:
            out[-1][1] += 1
        else:
            out.append([v, 1])
    return out


def _flags(values: Iterable[bool]) -> str:
    return "".join("1" if v else "0" for v in values)


def to_json(read: ScreenRead, cc_version: str = "unknown") -> dict[str, Any]:
    """The fixture form: per line ``text`` (one string per cell), ``faint`` / ``inverse``
    (``0``/``1`` per cell) and ``fg`` (``[descriptor, run length]`` runs)."""
    return {
        "cc_version": cc_version,
        "cols": read.cols,
        "cursor": [read.cursor[0], read.cursor[1]],
        "lines": [
            {
                "text": [c.ch for c in cells],
                "faint": _flags(c.faint for c in cells),
                "inverse": _flags(c.inverse for c in cells),
                "fg": _runs(c.fg for c in cells),
            }
            for cells in read.lines
        ],
    }


def from_json(data: dict[str, Any]) -> ScreenRead:
    """Inverse of :func:`to_json`."""
    lines = []
    for row in data["lines"]:
        texts = list(row["text"])
        fgs = [str(desc) for desc, n in row.get("fg", []) for _ in range(int(n))]
        faint = row.get("faint", "")
        inverse = row.get("inverse", "")
        cells = tuple(
            ScreenCell(
                ch=str(t),
                faint=i < len(faint) and faint[i] == "1",
                inverse=i < len(inverse) and inverse[i] == "1",
                fg=fgs[i] if i < len(fgs) else "",
            )
            for i, t in enumerate(texts)
        )
        lines.append(cells)
    cx, cy = data.get("cursor", [0, -1])
    return ScreenRead(int(data["cols"]), (int(cx), int(cy)), tuple(lines))
