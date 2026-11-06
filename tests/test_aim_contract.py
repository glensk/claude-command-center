"""Contract tests: `ccc aim` stdout/stderr/exit bytes from a REAL process (tp#1758 B3).

Pinned BEFORE the lean entry point (B4) so that refactor has to reproduce today's bytes
exactly: every ``--format`` against every session shape, how the session is picked
(explicit, env, the live registry by cwd), ``-h``, argparse errors, a missing / old /
corrupt DB and a reader that closed stdout. The expected values were captured from the
pre-B4 ``cli.cmd_aim``; a change here is a behaviour change of the status line.
"""

# The golden byte table is kept verbatim (one literal per case), so its lines run long.
# ruff: noqa: E501
# pylint: disable=line-too-long
# pylint: disable=redefined-outer-name  # pytest fixtures are injected by parameter name

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from command_center.store import Store

ROOT = Path(__file__).resolve().parents[1]

SID = "11111111-aaaa-4bbb-8ccc-000000000001"
OTHER = "22222222-aaaa-4bbb-8ccc-000000000002"
AIM = "ship the light refresh: tp#1758"
LONG_AIM = (
    "make every status-line render cheap enough that eleven live sessions do not "
    "load the machine: tp#1758"
)

# scenario -> session id (one DB holds them all; the id names the shape).
_SCENARIOS = (
    "no_row",
    "no_aim",
    "aim_no_checklist",
    "partial",
    "manual",
    "aim_met",
    "aim_met_no_progress",
    "deadline",
    "blocked",
    "everything",
    "long_aim",
    "multiline_aim",
)


def _sid(scenario: str) -> str:
    return f"sess-{scenario}"


def _seed(db: Path) -> None:
    """One store holding every scenario's session (``no_row`` deliberately absent)."""
    with Store(db) as store:
        store.ensure(_sid("no_aim"), cwd="/repo")
        for name in _SCENARIOS[2:]:
            store.ensure(_sid(name), cwd="/repo")
            store.set_aim(_sid(name), AIM)
        for name in ("partial", "aim_met", "everything", "long_aim"):
            store.set_subgoals(_sid(name), ["one", "two", "three"])
            first = store.conn.execute(
                "SELECT id FROM subgoals WHERE session_id = ? ORDER BY id LIMIT 1",
                (_sid(name),),
            ).fetchone()
            store.set_subgoal_checked(int(first[0]), True)
        store.update_fields(_sid("manual"), manual_progress=40)
        store.set_subgoals(_sid("manual"), ["one", "two"])
        store.set_aim_met(_sid("aim_met"), True, "verified", 1)
        store.set_aim_met(_sid("aim_met_no_progress"), True, "verified", 1)
        store.update_fields(_sid("deadline"), deadline="2026-12-01")
        store.update_fields(_sid("blocked"), blocked_on="review by Albert")
        store.update_fields(
            _sid("everything"), deadline="2026-12-01", blocked_on="review", manual_progress=75
        )
        store.set_aim_met(_sid("everything"), True, "verified", 1)
        store.set_aim(_sid("long_aim"), LONG_AIM)
        store.set_aim(_sid("multiline_aim"), "first line of the aim\nsecond line")


def _home(root: Path, *, main_line: bool | None) -> Path:
    """A CCC_HOME (and CLAUDE_HOME) with the seeded DB and optional `aim_on_main_line`."""
    app = root / "command-center"
    app.mkdir(parents=True)
    if main_line is not None:
        (app / "config.toml").write_text(
            f"aim_on_main_line = {'true' if main_line else 'false'}\n", encoding="utf-8"
        )
    _seed(app / "state.db")
    return root


@pytest.fixture(scope="module")
def homes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return {
        "default": _home(tmp_path_factory.mktemp("aim-default"), main_line=None),
        "off": _home(tmp_path_factory.mktemp("aim-off"), main_line=False),
        "on": _home(tmp_path_factory.mktemp("aim-on"), main_line=True),
    }


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "CLAUDE_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID",
            "CCC_HOME",
            "CLAUDE_HOME",
            "FORCE_COLOR",
            "CLAUDE_CODE_ENTRYPOINT",
            "CCC_INTERNAL",
        }
    }
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(
        HOME=str(home),
        CCC_HOME=str(home),
        CLAUDE_HOME=str(home),
        COLUMNS="80",
        NO_COLOR="1",
        PYTHON_COLORS="0",
        LC_ALL="en_US.UTF-8",
    )
    env.update(extra)
    return env


def _run(home: Path, *args: str, cwd: Path | None = None, **env: str) -> tuple[int, bytes, bytes]:
    proc = subprocess.run(
        [sys.executable, "-m", "command_center", "aim", *args],
        capture_output=True,
        env=_env(home, **env),
        cwd=cwd or home,
        timeout=60,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------- #
# every format × every session shape
# --------------------------------------------------------------------------- #
_FORMATS = {
    "statusline": ("default", ()),
    "default": ("default", ("--format", "statusline")),  # explicit == implicit
    "plain": ("default", ("--format", "plain")),
    "bar": ("default", ("--format", "bar")),
    "bar_off": ("off", ("--format", "bar")),
    "bar_on": ("on", ("--format", "bar")),
    "statusline_on": ("on", ()),  # aim_on_main_line touches the bar only
}

_EXPECTED: dict[tuple[str, str], bytes] = {}  # filled below (captured from pre-B4 ccc)
_EXPECTED.update(
    {
        ("statusline", "no_row"): b"\xf0\x9f\x8e\xaf set done-condition  (/aim)\n",
        ("statusline", "no_aim"): b"\xf0\x9f\x8e\xaf set done-condition  (/aim)\n",
        ("statusline", "aim_no_checklist"): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758\n",
        (
            "statusline",
            "partial",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91 33%\n",
        (
            "statusline",
            "manual",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91 40%\n",
        (
            "statusline",
            "aim_met",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \x1b[38;5;231m\xe2\x96\x88\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;231mD\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mO\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mN\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mE\x1b[0m\x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m 33%\n",
        (
            "statusline",
            "aim_met_no_progress",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mD\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mO\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mN\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mE\x1b[0m\x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "statusline",
            "deadline",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x8f\xb02026-12-01\n",
        (
            "statusline",
            "blocked",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x9b\x94review by Albert\n",
        (
            "statusline",
            "everything",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \x1b[38;5;231m\xe2\x96\x88\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;231mD\x1b[0m\x1b[1;38;5;196;48;5;231mO\x1b[0m\x1b[1;38;5;196;48;5;231mN\x1b[0m\x1b[1;38;5;196;48;5;231mE\x1b[0m\x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m 75%  \xe2\x8f\xb02026-12-01  \xe2\x9b\x94review\n",
        (
            "statusline",
            "long_aim",
        ): b"\xf0\x9f\x8e\xaf make every status-line render cheap enough that eleven live sessions do not load the machine: tp#1758  \xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91 33%\n",
        ("statusline", "multiline_aim"): b"\xf0\x9f\x8e\xaf first line of the aim\nsecond line\n",
        ("default", "no_row"): b"\xf0\x9f\x8e\xaf set done-condition  (/aim)\n",
        ("default", "no_aim"): b"\xf0\x9f\x8e\xaf set done-condition  (/aim)\n",
        ("default", "aim_no_checklist"): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758\n",
        (
            "default",
            "partial",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91 33%\n",
        (
            "default",
            "manual",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91 40%\n",
        (
            "default",
            "aim_met",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \x1b[38;5;231m\xe2\x96\x88\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;231mD\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mO\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mN\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mE\x1b[0m\x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m 33%\n",
        (
            "default",
            "aim_met_no_progress",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mD\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mO\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mN\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mE\x1b[0m\x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "default",
            "deadline",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x8f\xb02026-12-01\n",
        (
            "default",
            "blocked",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x9b\x94review by Albert\n",
        (
            "default",
            "everything",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \x1b[38;5;231m\xe2\x96\x88\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;231mD\x1b[0m\x1b[1;38;5;196;48;5;231mO\x1b[0m\x1b[1;38;5;196;48;5;231mN\x1b[0m\x1b[1;38;5;196;48;5;231mE\x1b[0m\x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m 75%  \xe2\x8f\xb02026-12-01  \xe2\x9b\x94review\n",
        (
            "default",
            "long_aim",
        ): b"\xf0\x9f\x8e\xaf make every status-line render cheap enough that eleven live sessions do not load the machine: tp#1758  \xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91 33%\n",
        ("default", "multiline_aim"): b"\xf0\x9f\x8e\xaf first line of the aim\nsecond line\n",
        ("plain", "no_row"): b"",
        ("plain", "no_aim"): b"",
        ("plain", "aim_no_checklist"): b"ship the light refresh: tp#1758\n",
        ("plain", "partial"): b"ship the light refresh: tp#1758\n",
        ("plain", "manual"): b"ship the light refresh: tp#1758\n",
        ("plain", "aim_met"): b"ship the light refresh: tp#1758\n",
        ("plain", "aim_met_no_progress"): b"ship the light refresh: tp#1758\n",
        ("plain", "deadline"): b"ship the light refresh: tp#1758\n",
        ("plain", "blocked"): b"ship the light refresh: tp#1758\n",
        ("plain", "everything"): b"ship the light refresh: tp#1758\n",
        (
            "plain",
            "long_aim",
        ): b"make every status-line render cheap enough that eleven live sessions do not load the machine: tp#1758\n",
        ("plain", "multiline_aim"): b"first line of the aim\nsecond line\n",
        ("bar", "no_row"): b"\x1b[38;5;244m\xf0\x9f\x8e\xaf /aim\x1b[0m\n",
        ("bar", "no_aim"): b"\x1b[38;5;244m\xf0\x9f\x8e\xaf /aim\x1b[0m\n",
        (
            "bar",
            "aim_no_checklist",
        ): b"\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "bar",
            "partial",
        ): b"\x1b[38;5;42m\xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\x1b[0m\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m 33%\n",
        (
            "bar",
            "manual",
        ): b"\x1b[38;5;42m\xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\x1b[0m\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m 40%\n",
        (
            "bar",
            "aim_met",
        ): b"\x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;42mD\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mO\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mN\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mE\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m 33%\n",
        (
            "bar",
            "aim_met_no_progress",
        ): b"\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mD\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mO\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mN\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mE\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\n",
        (
            "bar",
            "deadline",
        ): b"\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "bar",
            "blocked",
        ): b"\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "bar",
            "everything",
        ): b"\x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;42mD\x1b[0m\x1b[1;38;5;196;48;5;42mO\x1b[0m\x1b[1;38;5;196;48;5;42mN\x1b[0m\x1b[1;38;5;196;48;5;42mE\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m 75%\n",
        (
            "bar",
            "long_aim",
        ): b"\x1b[38;5;42m\xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\x1b[0m\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m 33%\n",
        (
            "bar",
            "multiline_aim",
        ): b"\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        ("bar_off", "no_row"): b"\x1b[38;5;244m\xf0\x9f\x8e\xaf /aim\x1b[0m\n",
        ("bar_off", "no_aim"): b"\x1b[38;5;244m\xf0\x9f\x8e\xaf /aim\x1b[0m\n",
        (
            "bar_off",
            "aim_no_checklist",
        ): b"\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "bar_off",
            "partial",
        ): b"\x1b[38;5;42m\xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\x1b[0m\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m 33%\n",
        (
            "bar_off",
            "manual",
        ): b"\x1b[38;5;42m\xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\x1b[0m\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m 40%\n",
        (
            "bar_off",
            "aim_met",
        ): b"\x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;42mD\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mO\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mN\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mE\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m 33%\n",
        (
            "bar_off",
            "aim_met_no_progress",
        ): b"\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mD\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mO\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mN\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mE\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\n",
        (
            "bar_off",
            "deadline",
        ): b"\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "bar_off",
            "blocked",
        ): b"\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "bar_off",
            "everything",
        ): b"\x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;42mD\x1b[0m\x1b[1;38;5;196;48;5;42mO\x1b[0m\x1b[1;38;5;196;48;5;42mN\x1b[0m\x1b[1;38;5;196;48;5;42mE\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m 75%\n",
        (
            "bar_off",
            "long_aim",
        ): b"\x1b[38;5;42m\xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\x1b[0m\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m 33%\n",
        (
            "bar_off",
            "multiline_aim",
        ): b"\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        ("bar_on", "no_row"): b"\x1b[38;5;244m\xf0\x9f\x8e\xaf /aim\x1b[0m\n",
        ("bar_on", "no_aim"): b"\x1b[38;5;244m\xf0\x9f\x8e\xaf /aim\x1b[0m\n",
        (
            "bar_on",
            "aim_no_checklist",
        ): b"\x1b[38;5;220m\xf0\x9f\x8e\xaf ship the light refresh: tp#1758\x1b[0m \x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "bar_on",
            "partial",
        ): b"\x1b[38;5;220m\xf0\x9f\x8e\xaf ship the light refresh: tp#1758\x1b[0m \x1b[38;5;42m\xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\x1b[0m\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m 33%\n",
        (
            "bar_on",
            "manual",
        ): b"\x1b[38;5;220m\xf0\x9f\x8e\xaf ship the light refresh: tp#1758\x1b[0m \x1b[38;5;42m\xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\x1b[0m\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m 40%\n",
        (
            "bar_on",
            "aim_met",
        ): b"\x1b[38;5;220m\xf0\x9f\x8e\xaf ship the light refresh: tp#1758\x1b[0m \x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;42mD\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mO\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mN\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mE\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m 33%\n",
        (
            "bar_on",
            "aim_met_no_progress",
        ): b"\x1b[38;5;220m\xf0\x9f\x8e\xaf ship the light refresh: tp#1758\x1b[0m \x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mD\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mO\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mN\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mE\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\n",
        (
            "bar_on",
            "deadline",
        ): b"\x1b[38;5;220m\xf0\x9f\x8e\xaf ship the light refresh: tp#1758\x1b[0m \x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "bar_on",
            "blocked",
        ): b"\x1b[38;5;220m\xf0\x9f\x8e\xaf ship the light refresh: tp#1758\x1b[0m \x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "bar_on",
            "everything",
        ): b"\x1b[38;5;220m\xf0\x9f\x8e\xaf ship the light refresh: tp#1758\x1b[0m \x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[38;5;42m\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;42mD\x1b[0m\x1b[1;38;5;196;48;5;42mO\x1b[0m\x1b[1;38;5;196;48;5;42mN\x1b[0m\x1b[1;38;5;196;48;5;42mE\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m\x1b[38;5;244m\xe2\x96\x91\x1b[0m 75%\n",
        (
            "bar_on",
            "long_aim",
        ): b"\x1b[38;5;220m\xf0\x9f\x8e\xaf make every status-line render cheap enough that el\xe2\x80\xa6: tp#1758\x1b[0m \x1b[38;5;42m\xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\x1b[0m\x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m 33%\n",
        (
            "bar_on",
            "multiline_aim",
        ): b"\x1b[38;5;220m\xf0\x9f\x8e\xaf first line of the aim\x1b[0m \x1b[38;5;244m\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        ("statusline_on", "no_row"): b"\xf0\x9f\x8e\xaf set done-condition  (/aim)\n",
        ("statusline_on", "no_aim"): b"\xf0\x9f\x8e\xaf set done-condition  (/aim)\n",
        (
            "statusline_on",
            "aim_no_checklist",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758\n",
        (
            "statusline_on",
            "partial",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91 33%\n",
        (
            "statusline_on",
            "manual",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91 40%\n",
        (
            "statusline_on",
            "aim_met",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \x1b[38;5;231m\xe2\x96\x88\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;231mD\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mO\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mN\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mE\x1b[0m\x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m 33%\n",
        (
            "statusline_on",
            "aim_met_no_progress",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mD\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mO\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mN\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mE\x1b[0m\x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m\n",
        (
            "statusline_on",
            "deadline",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x8f\xb02026-12-01\n",
        (
            "statusline_on",
            "blocked",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \xe2\x9b\x94review by Albert\n",
        (
            "statusline_on",
            "everything",
        ): b"\xf0\x9f\x8e\xaf ship the light refresh: tp#1758  \x1b[38;5;231m\xe2\x96\x88\xe2\x96\x88\x1b[0m\x1b[1;38;5;196;48;5;231mD\x1b[0m\x1b[1;38;5;196;48;5;231mO\x1b[0m\x1b[1;38;5;196;48;5;231mN\x1b[0m\x1b[1;38;5;196;48;5;231mE\x1b[0m\x1b[38;5;231m\xe2\x96\x91\xe2\x96\x91\x1b[0m 75%  \xe2\x8f\xb02026-12-01  \xe2\x9b\x94review\n",
        (
            "statusline_on",
            "long_aim",
        ): b"\xf0\x9f\x8e\xaf make every status-line render cheap enough that eleven live sessions do not load the machine: tp#1758  \xe2\x96\x93\xe2\x96\x93\xe2\x96\x93\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91\xe2\x96\x91 33%\n",
        (
            "statusline_on",
            "multiline_aim",
        ): b"\xf0\x9f\x8e\xaf first line of the aim\nsecond line\n",
    }
)


@pytest.mark.parametrize("scenario", _SCENARIOS)
@pytest.mark.parametrize("fmt", list(_FORMATS))
def test_aim_output_bytes(homes: dict[str, Path], fmt: str, scenario: str) -> None:
    home_key, fmt_args = _FORMATS[fmt]
    code, out, err = _run(homes[home_key], "--session", _sid(scenario), *fmt_args)
    assert (code, err) == (0, b"")
    assert out == _EXPECTED[(fmt, scenario)]


# --------------------------------------------------------------------------- #
# which session: explicit > CLAUDE_SESSION_ID > CLAUDE_CODE_SESSION_ID > registry by cwd
# --------------------------------------------------------------------------- #
def _sl(scenario: str) -> tuple[int, bytes, bytes]:
    return (0, _EXPECTED[("statusline", scenario)], b"")


def test_session_from_env(homes: dict[str, Path]) -> None:
    home = homes["default"]
    assert _run(home, CLAUDE_SESSION_ID=_sid("partial")) == _sl("partial")
    assert _run(home, CLAUDE_CODE_SESSION_ID=_sid("blocked")) == _sl("blocked")
    a, b = _sid("partial"), _sid("blocked")
    assert _run(home, CLAUDE_SESSION_ID=a, CLAUDE_CODE_SESSION_ID=b) == _sl("partial")
    assert _run(
        home, "--session", _sid("deadline"), CLAUDE_SESSION_ID=a, CLAUDE_CODE_SESSION_ID=b
    ) == _sl("deadline")


def test_session_from_registry_by_cwd(
    homes: dict[str, Path], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No id anywhere: the live registry entry in this cwd (busy first, then newest)."""
    home = homes["default"]
    work = tmp_path_factory.mktemp("aim-cwd")
    elsewhere = tmp_path_factory.mktemp("aim-elsewhere")
    reg = home / "sessions"
    reg.mkdir(exist_ok=True)
    entries = {
        "101": {"sessionId": _sid("partial"), "cwd": str(work), "status": "idle", "updatedAt": 9},
        "102": {"sessionId": _sid("blocked"), "cwd": str(work), "status": "busy", "updatedAt": 1},
        "103": {"sessionId": _sid("manual"), "cwd": str(work), "status": "idle", "updatedAt": 5},
    }
    try:
        for pid, data in entries.items():
            (reg / f"{pid}.json").write_text(json.dumps({"pid": int(pid), **data}), "utf-8")
        assert _run(home, cwd=work) == _sl("blocked")
        (reg / "102.json").unlink()
        assert _run(home, cwd=work) == _sl("partial")
        # Nothing resolvable: silent success, the status line just shows nothing.
        assert _run(home, cwd=elsewhere) == (0, b"", b"")
        assert _run(home, "--format", "bar", cwd=elsewhere) == (0, b"", b"")
    finally:
        for path in reg.glob("*.json"):
            path.unlink()


# --------------------------------------------------------------------------- #
# -h and argparse errors
# --------------------------------------------------------------------------- #
_USAGE = b"usage: ccc aim [-h] [--session SESSION] [--format {statusline,plain,bar}]\n"
_HELP = (
    _USAGE + b"\noptions:\n"
    b"  -h, --help            show this help message and exit\n"
    b"  --session SESSION\n"
    b"  --format {statusline,plain,bar}\n"
)
_ROOT_USAGE = b"usage: ccc [-h] [--version] {aim} ...\n"


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help(homes: dict[str, Path], flag: str) -> None:
    assert _run(homes["default"], flag) == (0, _HELP, b"")


@pytest.mark.parametrize(
    ("args", "err"),
    [
        (
            ("--format", "nope"),
            _USAGE + b"ccc aim: error: argument --format: invalid choice: 'nope' "
            b"(choose from statusline, plain, bar)\n",
        ),
        (("--format",), _USAGE + b"ccc aim: error: argument --format: expected one argument\n"),
        (
            ("--session",),
            _USAGE + b"ccc aim: error: argument --session: expected one argument\n",
        ),
        (("--bogus",), _ROOT_USAGE + b"ccc: error: unrecognized arguments: --bogus\n"),
        (("extra",), _ROOT_USAGE + b"ccc: error: unrecognized arguments: extra\n"),
    ],
)
def test_malformed_options(homes: dict[str, Path], args: tuple[str, ...], err: bytes) -> None:
    assert _run(homes["default"], *args) == (2, b"", err)


# --------------------------------------------------------------------------- #
# DB states
# --------------------------------------------------------------------------- #
def test_missing_db_answers_like_no_row(tmp_path_factory: pytest.TempPathFactory) -> None:
    home = tmp_path_factory.mktemp("aim-missing")
    for fmt in ("statusline", "plain", "bar"):
        assert _run(home, "--session", _sid("partial"), "--format", fmt) == (
            0,
            _EXPECTED[(fmt, "no_row")],
            b"",
        )


def test_older_db_missing_a_column_is_migrated(tmp_path_factory: pytest.TempPathFactory) -> None:
    """A DB from before ``manual_progress``: the column comes back NULL (auto progress)."""
    home = _home(tmp_path_factory.mktemp("aim-old"), main_line=None)
    db = home / "command-center" / "state.db"
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE sessions DROP COLUMN manual_progress")
    con.commit()
    con.close()
    want = {
        ("statusline", "manual"): "🎯 ship the light refresh: tp#1758  ░░░░░░░░ 0%\n".encode(),
        ("plain", "manual"): b"ship the light refresh: tp#1758\n",
        ("bar", "manual"): ("\x1b[38;5;42m\x1b[0m\x1b[38;5;244m░░░░░░░░\x1b[0m 0%\n".encode()),
        ("statusline", "everything"): (
            "🎯 ship the light refresh: tp#1758  \x1b[38;5;231m██\x1b[0m"
            "\x1b[1;38;5;196;48;5;231mD\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mO\x1b[0m"
            "\x1b[1;38;5;196;48;2;64;64;64mN\x1b[0m\x1b[1;38;5;196;48;2;64;64;64mE\x1b[0m"
            "\x1b[38;5;231m░░\x1b[0m 33%  ⏰2026-12-01  ⛔review\n"
        ).encode(),
        ("plain", "everything"): b"ship the light refresh: tp#1758\n",
        ("bar", "everything"): (
            "\x1b[38;5;42m█\x1b[0m\x1b[38;5;42m█\x1b[0m\x1b[1;38;5;196;48;5;42mD\x1b[0m"
            "\x1b[1;38;5;196;48;2;32;32;32mO\x1b[0m\x1b[1;38;5;196;48;2;32;32;32mN\x1b[0m"
            "\x1b[1;38;5;196;48;2;32;32;32mE\x1b[0m\x1b[38;5;244m░\x1b[0m"
            "\x1b[38;5;244m░\x1b[0m 33%\n"
        ).encode(),
    }
    for (fmt, scenario), out in want.items():
        assert _run(home, "--session", _sid(scenario), "--format", fmt) == (0, out, b""), (
            fmt,
            scenario,
        )
    con = sqlite3.connect(db)
    cols = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
    con.close()
    assert "manual_progress" in cols


def _last_line(err: bytes) -> bytes:
    return err.rstrip(b"\n").rsplit(b"\n", 1)[-1]


def test_ancient_db_fails_loudly(tmp_path_factory: pytest.TempPathFactory) -> None:
    """A sessions table too old to migrate: a traceback, exit 1, nothing on stdout.

    Only the exception line is pinned (the frames carry install paths and line numbers).
    """
    home = tmp_path_factory.mktemp("aim-ancient")
    (home / "command-center").mkdir()
    con = sqlite3.connect(home / "command-center" / "state.db")
    con.executescript(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, cwd TEXT NOT NULL DEFAULT '',"
        " agent TEXT NOT NULL DEFAULT 'claude', aim TEXT);"
        f"INSERT INTO sessions (session_id, cwd, aim) VALUES ('{SID}', '/repo', 'old aim');"
    )
    con.commit()
    con.close()
    for fmt in ("statusline", "plain", "bar"):
        code, out, err = _run(home, "--session", SID, "--format", fmt)
        assert (code, out) == (1, b"")
        assert err.startswith(b"Traceback (most recent call last):\n")
        assert _last_line(err) == b"sqlite3.OperationalError: no such column: archived"


def test_corrupt_db_fails_loudly(tmp_path_factory: pytest.TempPathFactory) -> None:
    home = tmp_path_factory.mktemp("aim-corrupt")
    (home / "command-center").mkdir()
    (home / "command-center" / "state.db").write_bytes(b"this is not a sqlite database" * 100)
    code, out, err = _run(home, "--session", SID)
    assert (code, out) == (1, b"")
    assert err.startswith(b"Traceback (most recent call last):\n")
    assert _last_line(err) == b"sqlite3.DatabaseError: file is not a database"


# --------------------------------------------------------------------------- #
# a reader that closed stdout before we wrote
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("args", [("--session", _sid("partial")), ("-h",)])
def test_dead_stdout_is_a_silent_sigpipe_exit(
    homes: dict[str, Path], args: tuple[str, ...]
) -> None:
    home = homes["default"]
    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-m", "command_center", "aim", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(home),
        cwd=home,
    )
    assert proc.stdout is not None
    proc.stdout.close()  # the only read end: every write now hits EPIPE
    _out, err = proc.communicate(timeout=60)
    assert (proc.returncode, err) == (141, b"")


# --------------------------------------------------------------------------- #
# the `ccc` console script answers byte-for-byte like `python -m command_center`
# --------------------------------------------------------------------------- #
def test_console_script_matches_module(homes: dict[str, Path]) -> None:
    script = Path(sys.executable).with_name("ccc")
    if not script.exists():
        pytest.skip("no `ccc` console script next to this interpreter")
    home = homes["on"]
    for args in (
        ("--session", _sid("everything")),
        ("--session", _sid("long_aim"), "--format", "bar"),
        ("--session", _sid("no_aim"), "--format", "bar"),
        ("-h",),
        ("--format", "nope"),
    ):
        proc = subprocess.run(
            [str(script), "aim", *args],
            capture_output=True,
            env=_env(home),
            cwd=home,
            timeout=60,
            check=False,
        )
        assert (proc.returncode, proc.stdout, proc.stderr) == _run(home, *args), args
