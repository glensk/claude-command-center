"""tp#1844 B3: ``composer.read_composer`` / ``parse_screen`` classify the input box."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from command_center import composer
from command_center.composer import Composer, parse_screen, read_composer
from command_center.screenread import ScreenCell, ScreenRead, from_json

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "composer"
_COLS = 40
_RULE = "─" * _COLS


def _readme_rows() -> list[tuple[str, str, int, str]]:
    """``(fixture, state, chars, mode)`` per row of the fixtures README table."""
    rows = []
    pattern = re.compile(r"^\| `(\w+)\.json` \| [^|]+ \| (\w+) \| (\d+) \| (\w+) \|")
    for line in (_FIXTURES / "README.md").read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if m:
            rows.append((m.group(1), m.group(2), int(m.group(3)), m.group(4)))
    return rows


_README = _readme_rows()


def _load(name: str) -> ScreenRead:
    return from_json(json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8")))


def _line(
    text: str, faint: str = "", inverse: str = "", cols: int = _COLS
) -> tuple[ScreenCell, ...]:
    """Cells of *text* padded with ``""`` to *cols*; ``faint`` / ``inverse``: ``0/1`` per cell."""
    chars = list(text) + [""] * (cols - len(text))
    return tuple(
        ScreenCell(
            ch=ch,
            faint=x < len(faint) and faint[x] == "1",
            inverse=x < len(inverse) and inverse[x] == "1",
        )
        for x, ch in enumerate(chars)
    )


def _screen(*lines: tuple[ScreenCell, ...], cursor: tuple[int, int] = (2, -1)) -> ScreenRead:
    return ScreenRead(_COLS, cursor, tuple(lines))


def test_input_guard_parse_readme_lists_every_fixture() -> None:
    names = {p.stem for p in _FIXTURES.glob("*.json")}
    assert names == {row[0] for row in _README}
    assert len(names) == 11


@pytest.mark.parametrize(("name", "state", "chars", "mode"), _README, ids=[r[0] for r in _README])
def test_input_guard_parse_fixture(name: str, state: str, chars: int, mode: str) -> None:
    assert read_composer(_load(name)) == Composer(state, chars, mode)  # type: ignore[arg-type]


def test_input_guard_parse_busy_queued_is_empty() -> None:
    """Hard acceptance (obj 6): the queued message above the top rule is not box content."""
    assert read_composer(_load("busy_queued")) == Composer("empty", 0, "prompt")


def test_input_guard_parse_statusline_short_rule_is_not_a_box_rule() -> None:
    read = _screen(
        _line(_RULE),
        _line("❯ "),
        _line(_RULE),
        _line("  status │ x"),
        _line("  ── short ──"),
    )
    assert read_composer(read) == Composer("empty", 0, "prompt")


def test_input_guard_parse_only_the_last_rule_pair_counts() -> None:
    read = _screen(
        _line(_RULE),
        _line("❯ conversation text above"),
        _line(_RULE),
        _line("more conversation"),
        _line(_RULE),
        _line("❯ "),
        _line(_RULE),
    )
    assert read_composer(read) == Composer("empty", 0, "prompt")


def test_input_guard_parse_bottom_rule_without_top_rule_is_text() -> None:
    read = _screen(_line("  ab cd"), _line("  ef"), _line(_RULE), _line("  status"))
    result = read_composer(read)
    assert result.state == "text"
    assert result.chars == 6


def test_input_guard_parse_no_rule_is_unrecognized() -> None:
    read = _screen(_line("❯ hello"), _line("  status"))
    assert read_composer(read).state == "unrecognized"


def test_input_guard_parse_empty_screen_is_unrecognized() -> None:
    assert read_composer(_screen()).state == "unrecognized"


def test_input_guard_parse_adjacent_rules_are_unrecognized() -> None:
    assert read_composer(_screen(_line(_RULE), _line(_RULE))).state == "unrecognized"


def test_input_guard_parse_unknown_glyph_is_unrecognized() -> None:
    read = _screen(_line(_RULE), _line("» hello"), _line(_RULE))
    assert read_composer(read).state == "unrecognized"


def test_input_guard_parse_legacy_gt_glyph_is_prompt() -> None:
    read = _screen(_line(_RULE), _line("> hi"), _line(_RULE))
    assert read_composer(read) == Composer("text", 2, "prompt")


def test_input_guard_parse_memory_glyph_is_text_without_chars() -> None:
    read = _screen(_line(_RULE), _line("# "), _line(_RULE))
    assert read_composer(read) == Composer("text", 0, "memory")


def test_input_guard_parse_inverse_blank_cursor_is_not_content() -> None:
    read = _screen(_line(_RULE), _line("❯  ", inverse="001"), _line(_RULE), cursor=(2, 1))
    assert read_composer(read) == Composer("empty", 0, "prompt")


def test_input_guard_parse_inverse_cursor_on_dim_placeholder_is_empty() -> None:
    """obj 1: Ink's inverse cursor on a placeholder's first letter, the rest faint."""
    read = _screen(
        _line(_RULE),
        _line("❯ Try this", faint="0001111111", inverse="001"),
        _line(_RULE),
        cursor=(2, 1),
    )
    assert read_composer(read) == Composer("empty", 0, "prompt")


def test_input_guard_parse_inverse_cell_off_cursor_counts() -> None:
    read = _screen(
        _line(_RULE),
        _line("❯ Try this", faint="0001111111", inverse="001"),
        _line(_RULE),
        cursor=(5, 1),
    )
    assert read_composer(read) == Composer("text", 1, "prompt")


def test_input_guard_parse_inverse_cursor_before_real_text_counts() -> None:
    read = _screen(_line(_RULE), _line("❯ abc", inverse="001"), _line(_RULE), cursor=(2, 1))
    assert read_composer(read) == Composer("text", 3, "prompt")


def test_input_guard_parse_inverse_cursor_after_real_text_is_text() -> None:
    read = _screen(
        _line(_RULE),
        _line("❯ abXtail", faint="000001111", inverse="00001"),
        _line(_RULE),
        cursor=(4, 1),
    )
    assert read_composer(read) == Composer("text", 2, "prompt")


def test_input_guard_parse_trailing_blanks_are_not_content() -> None:
    read = _screen(_line(_RULE), _line("❯ ab \x00\x00   \t"), _line(_RULE))
    assert read_composer(read) == Composer("text", 2, "prompt")


def test_input_guard_parse_wide_chars_and_emoji_count_per_cell() -> None:
    """obj 9: a wide character fills two columns, its tail cell ``""`` is blank."""
    cells = ["❯", " ", "日", "", "本", "", " ", "👍", "", "👨‍👩‍👧", ""]
    line = tuple(ScreenCell(ch) for ch in cells) + tuple(
        ScreenCell("") for _ in range(_COLS - len(cells))
    )
    read = _screen(_line(_RULE), line, _line(_RULE), cursor=(11, 1))
    assert read_composer(read) == Composer("text", 4, "prompt")


def test_input_guard_parse_cursor_above_lines_is_ignored() -> None:
    read = _screen(_line(_RULE), _line("❯ y"), _line(_RULE), cursor=(3, -1))
    assert read_composer(read) == Composer("text", 1, "prompt")


def test_input_guard_parse_cursor_past_line_is_ignored() -> None:
    read = _screen(_line(_RULE), _line("❯ y", inverse="001"), _line(_RULE), cursor=(99, 1))
    assert read_composer(read) == Composer("text", 1, "prompt")


def test_input_guard_parse_multi_line_continuation_counts() -> None:
    read = _screen(_line(_RULE), _line("❯ ab"), _line("  cd"), _line("❯ e"), _line(_RULE))
    assert read_composer(read) == Composer("text", 6, "prompt")


def test_input_guard_parse_screen_none_is_unreadable() -> None:
    assert parse_screen(None) == Composer("unreadable")


def test_input_guard_parse_screen_passes_through() -> None:
    assert parse_screen(_load("draft_one_line")) == Composer("text", 28, "prompt")


def test_input_guard_parse_exception_is_typed_and_never_leaks_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "aaaa secret draft aaaa"

    def boom(_read: ScreenRead) -> Composer:
        raise ValueError(secret)

    monkeypatch.setattr(composer, "read_composer", boom)
    result = parse_screen(_load("draft_one_line"))
    assert result == Composer("unreadable", 0, "unknown", "ValueError")
    assert secret not in repr(result)


def test_input_guard_parse_malformed_read_is_unreadable() -> None:
    bad = ScreenRead(_COLS, (0, 0), (None,))  # type: ignore[arg-type]
    assert parse_screen(bad).state == "unreadable"
