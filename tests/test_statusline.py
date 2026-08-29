"""Tests for the ``ccc statusline`` rows — especially the one-line todos strip."""

from __future__ import annotations

import io
from argparse import Namespace
from pathlib import Path

import pytest

from command_center import accounts, cli, config
from command_center.models import dumps_todos
from command_center.store import Store


def _seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, todos: list[tuple[str, str]] | None
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", aim="all tests pass", aim_score=80)  # specific => not red
    if todos is not None:
        store.update_fields("s1", todos=dumps_todos(todos))
    store.close()


def test_statusline_todos_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(
        tmp_path,
        monkeypatch,
        [("completed", "do a"), ("in_progress", "do b"), ("pending", "do c")],
    )
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    assert "1/3" in out  # one completed of three, counter on the left
    assert "☒" in out and "◧" in out and "☐" in out  # the three box glyphs
    # All items live on ONE line (a single print) — find it by the counter.
    todo_line = next(ln for ln in out.splitlines() if "1/3" in ln)
    assert "do a" in todo_line and "do c" in todo_line


def test_statusline_no_todos_no_extra_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path, monkeypatch, todos=None)
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    assert "☐" not in out and "☒" not in out  # no todo strip
    assert out.count("\n") == 2  # only the aim row + the status row


def test_statusline_derives_session_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With NO --session flag, the id comes from the status-line JSON on stdin."""
    _seed(tmp_path, monkeypatch, todos=None)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "s1", "cwd": "/repo"}'))
    cli.cmd_statusline(Namespace(session=None))
    out = capsys.readouterr().out
    assert "untracked" not in out  # it resolved a real tracked session
    assert "all tests pass" in out  # ...and printed that session's AIM


def test_statusline_low_aim_score_colors_chip_not_aim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    aim = "improve the project"
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", aim=aim, aim_score=20)
    store.close()
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    red = "\033[38;5;196m"
    reset = "\033[0m"
    assert f"{red}20%{reset} {aim}" in out
    assert f"{red}{aim}{reset}" not in out


def test_statusline_renders_short_aim_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With a short label generated, the /aim row shows it instead of the full AIM."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    full = "if Codex quota is exhausted and a session uses the codex workflow then show 😴"
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", full)
    store.set_short_aim("s1", "codex-wait status icon")
    store.close()
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    assert "codex-wait status icon" in out
    assert full not in out  # the long form stays out of the status line


def test_statusline_no_stale_short_aim_after_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An AIM change clears the old label — the CURRENT row shows the NEW full AIM, never
    the previous revision's short label (only the latest short aim may render there; the
    `/aim (1)` anchor row legitimately carries revision (1)'s own label)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", "first goal: pytest -q green")
    store.set_short_aim("s1", "old short label")
    store.set_aim("s1", "second goal: ruff and mypy clean")  # clears short_aim
    store.update_fields("s1", aim_prev=None)  # steady state
    store.close()
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    current_row = next(ln for ln in out.splitlines() if "/aim (2):" in ln)
    assert "old short label" not in current_row
    assert "second goal: ruff and mypy clean" in current_row


def test_statusline_shows_aim_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the AIM changed this turn, the row shows `old ====> /aim new`."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields(
        "s1",
        aim="tf chord toggles finished; pytest -q green",
        aim_score=80,
        aim_prev="toggle finished alias and make help nicer",
    )
    store.close()
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    assert "====>" in out  # the transition arrow
    assert "toggle finished alias and make help nicer" in out  # the old AIM
    assert "tf chord toggles finished" in out  # the new AIM


def test_statusline_keeps_long_aim_transition_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even a long transition stays on ONE row — old, arrow, full new AIM and bar together."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    long_new = (
        "every changed file passes ruff and mypy, pytest -q is green, "
        "and PR #42 is merged into main without conflicts"
    )
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", aim=long_new, aim_score=85, aim_prev="improve things")
    store.close()
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if "====>" in ln)
    # Old AIM, arrow and the WHOLE new AIM share the single row — nothing spilled over.
    assert "improve things" in row
    assert long_new in row
    # Only the 2 base rows (aim + status) -> it did not wrap.
    assert len([ln for ln in out.splitlines() if ln.strip()]) == 2


def test_statusline_shows_running_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The /aim prefix carries the current AIM's running number: /aim (N):."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", "vague start")
    store.set_aim("s1", "concrete: pytest -q green")  # second AIM => running index 2
    store.update_fields("s1", aim_prev=None)  # steady state: no this-turn transition
    store.close()
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    assert "/aim (2):" in out
    assert "====>" not in out


def test_statusline_transition_carries_indices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A change this turn shows the prior and current running numbers across the arrow."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", "vague start")
    store.set_aim("s1", "concrete: pytest -q green")
    store.close()
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    assert "====>" in out
    assert "/aim (1):" in out  # the prior AIM
    assert "/aim (2)" in out  # the current AIM, after the arrow


def test_statusline_index_falls_back_to_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An AIM that predates history tracking (no rows) is the first: /aim (1):."""
    _seed(tmp_path, monkeypatch, todos=None)  # AIM set via update_fields => no history rows
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    assert "/aim (1):" in out


def test_aim_history_cli_lists_progression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ccc aim-history` prints every revision oldest→newest, marking the current one."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", "vague start")
    store.set_aim("s1", "concrete: pytest -q green")
    store.close()
    cli.cmd_aim_history(Namespace(session="s1"))
    out = capsys.readouterr().out
    assert "2 revisions" in out
    assert "vague start" in out
    assert "concrete: pytest -q green" in out
    assert "← current" in out


def test_aim_bar_format_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ccc aim --format bar` renders a compact filled/empty bar + percentage."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", aim="ship it", aim_score=80)
    store.set_subgoals("s1", ["a", "b", "c", "d"])
    store.set_subgoal_checked(store.list_subgoals("s1")[0].id, True)  # 1/4 = 25%
    store.close()
    cli.cmd_aim(Namespace(session="s1", format="bar"))
    out = capsys.readouterr().out
    assert "▓" in out and "░" in out  # both filled and empty cells present
    assert out.count("▓") == 2 and out.count("░") == 6  # round(0.25*8)=2 filled
    assert "25%" in out


def test_aim_bar_format_no_checklist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AIM set but no checklist → an all-empty bar, no percentage."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", aim="ship it", aim_score=80)
    store.close()
    cli.cmd_aim(Namespace(session="s1", format="bar"))
    out = capsys.readouterr().out
    assert "░" in out and "▓" not in out and "%" not in out


def test_aim_bar_format_no_aim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No AIM yet → a dim `/aim` placeholder (line 1 never goes blank)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.close()
    cli.cmd_aim(Namespace(session="s1", format="bar"))
    out = capsys.readouterr().out
    assert "/aim" in out and "▓" not in out


def test_effort_from_statusline_payload() -> None:
    """The pure extractor accepts the top-level and nested model keys; validates the level."""
    assert cli._effort_from_statusline_payload({"effort": "xhigh"}) == "xhigh"
    assert cli._effort_from_statusline_payload({"effortLevel": "high"}) == "high"
    assert cli._effort_from_statusline_payload({"reasoningEffort": "medium"}) == "medium"
    assert cli._effort_from_statusline_payload({"model": {"reasoning_effort": "low"}}) == "low"
    assert cli._effort_from_statusline_payload({"model": {"effort": "xhigh"}}) == "xhigh"
    assert cli._effort_from_statusline_payload({"effort": "bogus"}) is None  # invalid level
    assert cli._effort_from_statusline_payload({}) is None  # no key → no-op (today's payload)


def test_capture_effort_from_payload_persists_on_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid effort in the payload is persisted; a no-key payload is a silent no-op."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.close()

    cli._capture_effort_from_payload({"session_id": "s1", "effort": "xhigh"})
    with Store() as store:
        assert store.get("s1").effort == "xhigh"  # type: ignore[union-attr]

    # No effort key → left unchanged (the common case on current Claude Code).
    cli._capture_effort_from_payload({"session_id": "s1"})
    with Store() as store:
        assert store.get("s1").effort == "xhigh"  # type: ignore[union-attr]

    # Unknown session id → silently ignored (never raises).
    cli._capture_effort_from_payload({"session_id": "nope", "effort": "high"})


def test_statusline_print_glyph_returns_immediately_without_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--print-glyph` is a standalone mode: no Store/session needed, prints nothing
    but the bare glyph (single-account here, so an empty string), exit 0."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert cli.cmd_statusline(Namespace(print_glyph=True, session=None)) == 0
    assert capsys.readouterr().out == ""


def test_statusline_print_glyph_reflects_the_identity_hard_link(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The exact bug report: a config dir named "private" whose CURRENT identity is
    "work" must print 💼, not 🏠 — `--print-glyph` reads `accounts.current_account_glyph`."""
    private_dir, work_dir = tmp_path / "private", tmp_path / "work"
    private_dir.mkdir()
    work_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HOME", str(private_dir))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        config, "claude_config_dirs", lambda: {"private": private_dir, "work": work_dir}
    )
    (tmp_path / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "work@example.com"}}')
    monkeypatch.setattr(config, "claude_account_email_map", lambda: {"work": "work@example.com"})
    assert cli.cmd_statusline(Namespace(print_glyph=True, session=None)) == 0
    assert capsys.readouterr().out == accounts._WORK_GLYPH.strip()


def test_statusline_always_shows_first_aim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Revision (1) stays on screen next to the current AIM, however often it is sharpened."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", "fix claude code statusline")
    store.set_aim("s1", "second aim")
    store.set_aim("s1", "fix AWS secret-key pattern")  # running index 3
    store.update_fields("s1", aim_prev=None)  # steady state: no this-turn transition
    store.close()
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    assert "/aim (1):" in out
    assert "fix claude code statusline" in out  # the AIM the user typed themselves
    assert "/aim (3):" in out
    assert "fix AWS secret-key pattern" in out


def test_statusline_first_aim_anchor_not_duplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A this-turn transition out of revision (1) already shows it — no second (1) row."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", "vague start")
    store.set_aim("s1", "concrete: pytest -q green")
    store.close()
    cli.cmd_statusline(Namespace(session="s1"))
    out = capsys.readouterr().out
    assert out.count("/aim (1):") == 1
