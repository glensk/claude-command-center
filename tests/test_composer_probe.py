"""tp#1844 B1: ``terminal.iterm_screen_tail`` and ``tools/composer_probe.py`` (read-only)."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from command_center import screenread, terminal
from command_center.screenread import ScreenCell, ScreenRead

_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "composer_probe.py"
_RULE = "─"


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("composer_probe", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Color:
    def __init__(self, standard: int | None = None, rgb: tuple[int, int, int] | None = None):
        self.is_standard = standard is not None
        self.is_rgb = rgb is not None
        self.is_alternate = False
        self.standard = standard
        self.rgb = types.SimpleNamespace(red=rgb[0], green=rgb[1], blue=rgb[2]) if rgb else None


class _Style:
    def __init__(self, faint: bool = False, inverse: bool = False, fg: _Color | None = None):
        self.faint = faint
        self.inverse = inverse
        self.fg_color = fg


class _Line:
    """A fake ``LineContents``: *cells* are per-column strings (``""`` = wide-char tail)."""

    def __init__(self, cells: list[str], styles: list[_Style] | None = None) -> None:
        self.cells = cells
        self.styles = styles or [_Style() for _ in cells]

    @property
    def string(self) -> str:
        return "".join(self.cells)

    def string_at(self, x: int) -> str:
        return self.cells[x]

    def style_at(self, x: int) -> _Style | None:
        return self.styles[x] if 0 <= x < len(self.styles) else None


def _text_line(text: str, cols: int, **style: Any) -> _Line:
    cells = list(text) + [" "] * (cols - len(text))
    return _Line(cells, [_Style(**style) for _ in cells])


class _Contents:
    def __init__(self, lines: list[_Line], cursor: tuple[int, int]) -> None:
        self._lines = lines
        self.cursor_coord = types.SimpleNamespace(x=cursor[0], y=cursor[1])

    @property
    def number_of_lines(self) -> int:
        return len(self._lines)

    def line(self, i: int) -> _Line:
        return self._lines[i]


class _FakeSession:
    def __init__(self, contents: _Contents, cols: int, path: str = "/tmp/tp1844-fixtures"):
        self.contents = contents
        self.grid_size = types.SimpleNamespace(width=cols, height=contents.number_of_lines)
        self.path = path
        self.reads = 0

    async def async_get_screen_contents(self) -> _Contents:
        self.reads += 1
        return self.contents

    async def async_get_variable(self, name: str) -> str | None:
        return self.path if name == "path" else None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("async_send") or name.startswith("async_inject"):
            raise AssertionError(f"the probe must never send ({name})")
        raise AttributeError(name)


def _fake_iterm2(
    monkeypatch: pytest.MonkeyPatch, session: _FakeSession | None, *, fail: bool = False
) -> None:
    class _App:
        def get_session_by_id(self, _uuid: str) -> _FakeSession | None:
            return session

    class _Connection:
        @staticmethod
        async def async_create() -> object:
            if fail:
                raise ConnectionRefusedError("api down")
            return object()

    async def async_get_app(_conn: object) -> _App:
        return _App()

    module = types.ModuleType("iterm2")
    module.Connection = _Connection  # type: ignore[attr-defined]
    module.async_get_app = async_get_app  # type: ignore[attr-defined]
    module.app = types.SimpleNamespace(invalidate_app=lambda: None)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "iterm2", module)
    monkeypatch.setattr(terminal, "_iterm_api_auth_is_tcc_free", lambda: True)


def _box_screen(cols: int = 20) -> _Contents:
    """Conversation, spinner, top rule, ``❯ Hi, 42!`` (cursor after it), bottom rule, status."""
    lines = [
        _text_line("old talk 1", cols),
        _text_line("old talk 2", cols),
        _text_line("old talk 3", cols),
        _text_line("old talk 4", cols),
        _text_line("* Working", cols),
        _text_line(_RULE * cols, cols),
        _text_line("❯ Hi, 42!", cols),
        _text_line(_RULE * cols, cols),
        _text_line("status 7", cols, faint=True, fg=_Color(standard=8)),
    ]
    return _Contents(lines, (9, 6))


def test_input_guard_probe_reads_cells_styles_and_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(_box_screen(), 20)
    _fake_iterm2(monkeypatch, session)
    read = terminal.iterm_screen_tail("w0t0p0:U")
    assert read is not None
    assert read.cols == 20
    assert read.cursor == (9, 6)
    assert len(read.lines) == 9
    assert screenread.line_text(read.lines[6]).rstrip() == "❯ Hi, 42!"
    status = read.lines[8][0]
    assert (status.faint, status.inverse, status.fg) == (True, False, "std:8")
    assert session.reads == 1


def test_input_guard_probe_keeps_only_the_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_iterm2(monkeypatch, _FakeSession(_box_screen(), 20))
    read = terminal.iterm_screen_tail("w0t0p0:U", lines=4)
    assert read is not None
    assert len(read.lines) == 4
    assert read.cursor == (9, 1)


def test_input_guard_probe_rgb_fg_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    line = _Line(["a"], [_Style(fg=_Color(rgb=(153, 153, 153)))])
    _fake_iterm2(monkeypatch, _FakeSession(_Contents([line], (0, 0)), 1))
    read = terminal.iterm_screen_tail("U")
    assert read is not None
    assert read.lines[0][0].fg == "rgb:999999"


def test_input_guard_probe_cells_are_per_column_with_wide_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "❯ 漢😀x": the CJK char and the emoji each take two columns, the second one "".
    cells = ["❯", " ", "漢", "", "😀", "", "x", " "]
    styles = [_Style() for _ in cells]
    styles[6] = _Style(inverse=True)
    _fake_iterm2(monkeypatch, _FakeSession(_Contents([_Line(cells, styles)], (6, 0)), 8))
    read = terminal.iterm_screen_tail("U")
    assert read is not None
    row = read.lines[0]
    assert [c.ch for c in row] == cells
    assert row[6].inverse is True and row[6].ch == "x"
    assert read.cursor == (6, 0)


def test_input_guard_probe_short_line_pads_missing_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_iterm2(monkeypatch, _FakeSession(_Contents([_Line(["a"])], (0, 0)), 3))
    read = terminal.iterm_screen_tail("U")
    assert read is not None
    assert [c.ch for c in read.lines[0]] == ["a", "", ""]


def test_input_guard_probe_none_when_connection_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_iterm2(monkeypatch, _FakeSession(_box_screen(), 20), fail=True)
    assert terminal.iterm_screen_tail("U") is None


def test_input_guard_probe_none_for_unknown_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_iterm2(monkeypatch, None)
    assert terminal.iterm_screen_tail("U") is None
    assert terminal.iterm_screen_tail("") is None


def test_input_guard_probe_never_sends(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = _FakeSession(_box_screen(), 20)
    _fake_iterm2(monkeypatch, session)
    probe = _load_probe()
    out = tmp_path / "f.json"
    assert probe.main(["-s", "U", "-o", str(out), "-c", "9.9.9"]) == 0
    with pytest.raises(AssertionError):
        _ = session.async_send_text
    assert session.reads == 1


def test_input_guard_probe_masks_everything_but_rules_and_glyphs() -> None:
    assert screenread.mask_char(_RULE) == _RULE
    for glyph in "❯>!#│╭╮╰╯":
        assert screenread.mask_char(glyph) == glyph
    for glyph in ">!#":  # away from the glyph column these are punctuation
        assert screenread.mask_char(glyph, 5) == "x"
    assert screenread.mask_char("❯", 5) == "❯"
    assert screenread.mask_char(" ") == " "
    assert screenread.mask_char("\x00") == "\x00"
    assert screenread.mask_char(" ") == " "
    assert screenread.mask_char("") == ""
    assert screenread.mask_char("Q") == "a"
    assert screenread.mask_char("é") == "a"
    assert screenread.mask_char("7") == "9"
    for punct in ",.?/:;'\"-_~*@😀":
        assert screenread.mask_char(punct) == "x"


def test_input_guard_probe_writes_masked_tail_from_top_rule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_iterm2(monkeypatch, _FakeSession(_box_screen(), 20))
    probe = _load_probe()
    out = tmp_path / "f.json"
    assert probe.main(["-s", "U", "-n", "1", "-o", str(out), "-c", "9.9.9"]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["cc_version"] == "9.9.9"
    assert data["cols"] == 20
    texts = ["".join(row["text"]).rstrip() for row in data["lines"]]
    # one row above the top rule, then the box and the status line; the talk is gone
    assert texts == ["x aaaaaaa", _RULE * 20, "❯ aax 99x", _RULE * 20, "aaaaaa 9"]
    assert data["cursor"] == [9, 2]
    assert data["lines"][4]["faint"] == "1" * 20
    assert data["lines"][4]["fg"] == [["std:8", 20]]
    assert data["lines"][2]["inverse"] == "0" * 20
    raw = out.read_text(encoding="utf-8")
    for secret in ("Hi", "42", "talk", "Working", "status"):
        assert secret not in raw


def test_input_guard_probe_without_rules_keeps_bottom_twenty() -> None:
    lines = tuple((ScreenCell("q"),) for _ in range(30))
    read = ScreenRead(1, (0, 3), lines)
    kept = screenread.tail_from_top_rule(read, 3)
    assert len(kept.lines) == 20
    assert kept.cursor == (0, -1)


def test_input_guard_probe_short_rule_is_not_a_box_rule() -> None:
    cols = 20
    short = tuple(ScreenCell(c) for c in ("── x ──" + " " * 13))
    full = tuple(ScreenCell(_RULE) for _ in range(cols))
    assert not screenread.is_full_rule(short, cols)
    assert screenread.is_full_rule(full, cols)


def test_input_guard_probe_json_round_trip() -> None:
    read = ScreenRead(
        3,
        (1, 0),
        (
            (
                ScreenCell("❯"),
                ScreenCell("a", faint=True, fg="std:8"),
                ScreenCell("", inverse=True),
            ),
        ),
    )
    assert screenread.from_json(json.loads(json.dumps(screenread.to_json(read)))) == read


def test_input_guard_probe_refuses_cwd_outside_tmp_tp1844(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeSession(_box_screen(), 20, path="/tmp/other-work")
    _fake_iterm2(monkeypatch, session)
    probe = _load_probe()
    assert probe.main(["-s", "U", "-c", "9.9.9"]) == 1
    assert "refused" in capsys.readouterr().err
    assert session.reads == 0


def test_input_guard_probe_cwd_allowed_rules() -> None:
    probe = _load_probe()
    assert probe.cwd_allowed("/tmp/tp1844-fixtures")
    assert probe.cwd_allowed("/private/tmp/tp1844-accept/sub")
    assert not probe.cwd_allowed("/tmp/tp1844")
    assert not probe.cwd_allowed("/tmp/other")
    assert not probe.cwd_allowed("/tmp/tp1844-x/../../etc")
    assert not probe.cwd_allowed("")


def test_input_guard_probe_refuses_when_api_down(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_iterm2(monkeypatch, _FakeSession(_box_screen(), 20), fail=True)
    probe = _load_probe()
    assert probe.main(["-s", "U", "-c", "9.9.9"]) == 1


def test_input_guard_probe_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    probe = _load_probe()
    with pytest.raises(SystemExit) as exc:
        probe.main(["-h"])
    assert exc.value.code == 0
    assert "--session" in capsys.readouterr().out
