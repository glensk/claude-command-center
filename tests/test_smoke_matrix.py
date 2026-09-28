"""CI wrapper for the pre-publish acceptance smoke matrix.

Invokes ``tools/smoke_matrix.py`` as a subprocess (it builds a wheel, spins a scratch
sandbox with a temp ``HOME``/``CLAUDE_HOME``, runs the acceptance commands, and proves the
real ``~/.claude`` state was untouched). Marked ``slow`` — it is exercised by a full
``pytest`` run but can be deselected with ``-m 'not slow'`` — and skipped when ``uv`` is
unavailable to build the wheel.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "tools" / "smoke_matrix.py"
_UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(_UV is None, reason="uv not available to build the wheel")


@pytest.mark.slow
def test_smoke_matrix_passes() -> None:
    """The end-to-end acceptance battery runs green and proves real state is untouched."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert result.returncode == 0, f"smoke matrix failed:\n{result.stdout}\n{result.stderr}"
    assert "RESULT: PASS" in result.stdout


def _load_smoke_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_matrix", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # @dataclass resolves its module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def test_daemon_quota_caches_are_churn_not_leaks() -> None:
    """tp#687: the live daemon rewrites these mid-run; flagging them made the matrix flaky."""
    mod = _load_smoke_module()
    churn = [
        "muse_usage.json",
        "opencode_usage.json",
        "agy_usage.json",
        "codex_usage-687eca66.json",
        "usage-work-b6f4d184.json",
        "profile-private-ebcf0c99.json",
        "jump_tui",
        "cooldowns.json",
        "codex-seat-attempts.json",
        "codex-runs.jsonl",
        "snapshots/20260830-164938.json",
        "codex-switch/17fdeb91eff8033c.json",
    ]
    for rel in churn:
        assert mod._is_volatile(rel), rel
    for rel in ("config.toml", "tags.toml", "store.db", "backup/config.toml"):
        assert not mod._is_volatile(rel), rel
    before = {"cc_map": dict.fromkeys(churn, 1), "cc_count": len(churn), "cc_newest": 1}
    after = {"cc_map": dict.fromkeys(churn, 2), "cc_count": len(churn), "cc_newest": 2}
    for d in (before, after):
        d.update(settings_stat_ns=1, settings_lstat_ns=1, settings_hash="h", settings_realpath="p")
    ok, notes = mod.compare_real_state(before, after)
    assert ok, notes
