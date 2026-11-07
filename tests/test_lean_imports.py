"""The stdlib-only status-line modules load no heavy module (tp#1806 B2).

``slrender``, ``rolemark``, ``livestatus`` and ``usagecache`` are what the lean
``ccc statusline`` path runs on; importing (and exercising) them in a fresh interpreter
must not pull in ``cli``, ``config``, ``models``, ``store``, ``usage``, ``park``,
``tabsymbol``, ``accounts``, ``adapters``, ``dataclasses``, ``argparse``, rich or textual.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from leanprobe import child_pythonpath, forbidden

LEAN = ("slrender", "rolemark", "livestatus", "usagecache")

_PROBE = """
import json, sys
from pathlib import Path
from command_center import livestatus, rolemark, slrender, usagecache
tmp = Path(sys.argv[2])
livestatus.derive_status_value(("busy", True), None)
livestatus.find_transcript([tmp / "projects"], "/repo", "sid")
livestatus.halt_record(livestatus.tail_records(tmp / "missing.jsonl"))
usagecache.write_usage(
    {"five_hour": {"used_percentage": 1, "resets_at": 4102444800}},
    account="private",
    app_home=tmp / "app",
    account_dir=tmp / "claude",
)
usagecache.account_from_env(usagecache.parse_claude_accounts([]))
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(sorted(sys.modules), fh)
"""


def test_lean_modules_load_no_heavy_module(tmp_path: Path) -> None:
    report = tmp_path / "modules.json"
    env = {k: v for k, v in os.environ.items() if k not in {"CCC_HOME", "CLAUDE_HOME"}}
    env.update(
        PYTHONPATH=child_pythonpath(env),
        HOME=str(tmp_path),
        CLAUDE_HOME=str(tmp_path / "claude"),
    )
    subprocess.run(
        [sys.executable, "-c", _PROBE, str(report), str(tmp_path)],
        capture_output=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=True,
    )
    modules = json.loads(report.read_text(encoding="utf-8"))
    assert all(f"command_center.{name}" in modules for name in LEAN)
    assert (tmp_path / "app" / "usage.json").is_file()  # the probe really wrote
    assert [m for m in modules if forbidden(m)] == []


def test_forbidden_matcher_spares_usagecache() -> None:
    assert not forbidden("command_center.usagecache")
    assert forbidden("command_center.usage")
    assert forbidden("command_center.adapters.claude")
    assert forbidden("rich.text")
