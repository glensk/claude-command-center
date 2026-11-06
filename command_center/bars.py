#!/usr/bin/env python3
"""Progress bars, the DONE overlay and the ``ccc aim`` line — stdlib only (tp#1758 B4).

The status line spawns ``ccc aim`` on every render of every live session, so the lean
entry point (:mod:`command_center.entrypoint`) renders it without importing ``models``,
``config``, ``store`` or ``cli``. Everything that draws a bar lives here and is
re-exported by :mod:`command_center.models` (and the ``_paint_*`` helpers by
:mod:`command_center.cli`), so every bar site keeps one glyph source.

Imports: nothing beyond the standard library; :func:`aim_line` pulls in
:mod:`command_center.aimtext` (``re`` only) when it clips an AIM onto the main line.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


def progress_fraction(checked: int, total: int) -> float | None:
    """Fraction of checklist items done, or ``None`` when there is no checklist."""
    if total <= 0:
        return None
    return checked / total


def effective_progress(manual: int | None, checked: int, total: int) -> float | None:
    """The fraction every progress bar shows: manual override else the checklist ratio.

    *manual* is the session's ``manual_progress`` (0..100, clamped; ``None`` = auto).
    A set value wins over the sub-goal ratio at EVERY bar site (TUI table + detail
    head, ``ccc ls``, the statusline and ``ccc aim``) — keep this the single chokepoint
    so the sites can never disagree.
    """
    if manual is not None:
        return min(100, max(0, manual)) / 100
    return progress_fraction(checked, total)


def progress_bar(fraction: float | None, width: int = 10) -> str:
    """Render a fixed-width bar; ``—`` (padded) when no checklist exists yet."""
    if fraction is None:
        return "—".ljust(width)
    fraction = min(1.0, max(0.0, fraction))
    filled = round(fraction * width)
    return "▓" * filled + "░" * (width - filled)


DONE_WORD = "DONE"

_XTERM_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
_XTERM_BASE_16 = {
    0: (0, 0, 0),
    1: (205, 0, 0),
    2: (0, 205, 0),
    3: (205, 205, 0),
    4: (0, 0, 238),
    5: (205, 0, 205),
    6: (0, 205, 205),
    7: (229, 229, 229),
    8: (127, 127, 127),
    9: (255, 0, 0),
    10: (0, 255, 0),
    11: (255, 255, 0),
    12: (92, 92, 255),
    13: (255, 0, 255),
    14: (0, 255, 255),
    15: (255, 255, 255),
}


def xterm_rgb(code: int) -> tuple[int, int, int]:
    """RGB of an xterm-256 colour index (6×6×6 cube, grayscale ramp, classic 16)."""
    if 16 <= code <= 231:
        idx = code - 16
        return (
            _XTERM_CUBE_LEVELS[idx // 36],
            _XTERM_CUBE_LEVELS[(idx % 36) // 6],
            _XTERM_CUBE_LEVELS[idx % 6],
        )
    if 232 <= code <= 255:
        v = 8 + 10 * (code - 232)
        return (v, v, v)
    return _XTERM_BASE_16.get(code, (128, 128, 128))


def empty_track_tint(glyph_rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Background RGB for a DONE letter over the *empty* (``░``) track: a faint 25 % tint.

    ``░`` inks 25 % of its cell in the track colour, so over a dark terminal a ░ cell averages
    to a quarter of the glyph colour — the closest a letter's (necessarily solid) background
    can get to the dotted empty texture around it.
    """
    r, g, b = glyph_rgb
    return (round(r * 0.25), round(g * 0.25), round(b * 0.25))


def done_bar_parts(
    fraction: float | None, width: int = 10
) -> tuple[str, str, str, tuple[bool, ...]]:
    """Split a progress bar into ``(left, "DONE", right, fills)`` — "model thinks done" overlay.

    ``DONE`` is stamped into the centre cells of the bar — so the fill proportion still reads
    on both sides — and *fills* says, per ``DONE`` letter, whether the cell it covers belongs
    to the filled part. The DONE bar renders its filled cells as SOLID ``█`` (not the ordinary
    bar's dotted ``▓``): a letter's background is one flat colour, so no shade glyph next to it
    can ever match exactly — with a solid fill, painting the letters on the very same fill
    colour makes letter cells and bar cells pixel-identical (empty-track letters get
    :func:`empty_track_tint`). When there is no checklist yet (``fraction is None``) the track
    is all empty cells so the verdict still renders (rather than the padded ``—``). The three
    string pieces always concatenate to exactly *width* characters, preserving column
    alignment; a *width* below ``len("DONE")`` degrades to a truncated centre with empty sides.
    """
    base = progress_bar(fraction, width) if fraction is not None else "░" * width
    base = base.replace("▓", "█")
    if width <= len(DONE_WORD):
        word = DONE_WORD[:width]
        return ("", word, "", tuple(ch == "█" for ch in base[: len(word)]))
    pos = (width - len(DONE_WORD)) // 2
    covered = base[pos : pos + len(DONE_WORD)]
    return (
        base[:pos],
        DONE_WORD,
        base[pos + len(DONE_WORD) :],
        tuple(ch == "█" for ch in covered),
    )


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


MAIN_LINE_AIM_W = 60  # chars of the AIM shown on status-line row 1


def aim_line(  # pylint: disable=too-many-return-statements  # one return per display mode
    fmt: str,
    aim: str | None,
    *,
    fraction: float | None = None,
    aim_met: bool = False,
    deadline: str | None = None,
    blocked_on: str | None = None,
    main_line: bool = False,
) -> str:
    """The whole stdout of ``ccc aim --format <fmt>`` for one session ("" = print nothing).

    *aim* ``None``/empty is "no row or no AIM yet": a placeholder for ``statusline`` and
    ``bar``, nothing for ``plain``. *main_line* (``aim_on_main_line``) prefixes the bar with
    the clipped AIM. Shared by ``cli.cmd_aim`` and the lean entry point so both print the
    same bytes.
    """
    if not aim:
        if fmt == "statusline":
            return "🎯 set done-condition  (/aim)\n"
        if fmt == "bar":
            return "\033[38;5;244m🎯 /aim\033[0m\n"  # main-line placeholder, no AIM yet
        return ""
    if fmt == "plain":
        return f"{aim}\n"
    pct = f" {int(round(fraction * 100))}%" if fraction is not None else ""
    if fmt == "bar":
        head = ""
        if main_line:
            from . import aimtext  # lazy: the item run is kept whole (tp#1366)

            head = f"\033[38;5;220m🎯 {aimtext.clip(aim, MAIN_LINE_AIM_W)}\033[0m "
        # Compact, colored progress bar for the main status line (line 1). Filled
        # cells green, empty dim; ``progress_bar`` stays the single glyph source.
        green, dim, reset = "\033[38;5;42m", "\033[38;5;244m", "\033[0m"
        if aim_met:
            # Impartial checker judged the AIM fulfilled → red DONE stamped inside the
            # bar; letters over filled cells carry the green fill as background.
            left, done_word, right, fills = done_bar_parts(fraction, 8)
            bar_text = _paint_bar_glyphs(left, green, dim, reset)
            bar_text += _paint_done_word(done_word, fills, 42, 244)
            bar_text += _paint_bar_glyphs(right, green, dim, reset)
            return f"{head}{bar_text}{pct}\n"
        if fraction is None:
            return f"{head}{dim}{'░' * 8}{reset}\n"  # AIM set, no checklist yet
        filled = progress_bar(fraction, 8).count("▓")
        bar_text = f"{green}{'▓' * filled}{reset}{dim}{'░' * (8 - filled)}{reset}"
        return f"{head}{bar_text}{pct}\n"
    parts = [f"🎯 {aim}"]
    if aim_met:  # red DONE stamped inside the bar (same overlay as every bar site)
        left, done_word, right, fills = done_bar_parts(fraction, 8)
        # The DONE bar's glyph runs are painted 231 (white) — the SAME palette entry the
        # filled letters use as background — so █ cells and letter cells match exactly.
        white = "\033[38;5;231m"
        parts.append(
            f"{white}{left}\033[0m"
            f"{_paint_done_word(done_word, fills, 231, 231)}"
            f"{white}{right}\033[0m{pct}"
        )
    elif fraction is not None:
        parts.append(f"{progress_bar(fraction, 8)}{pct}")
    if deadline:
        parts.append(f"⏰{deadline}")
    if blocked_on:
        parts.append(f"⛔{blocked_on}")
    return "  ".join(parts) + "\n"
