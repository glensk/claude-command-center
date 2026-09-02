"""The public-mirror gate runs inside pytest, so a personal anchor fails the suite.

``tools/check_public_tree.py`` is the contract (see AGENTS.md, "The public-tree gate"),
but a script nobody runs cannot hold the line: seven hits accumulated between 2026-08-31
and 2026-09-01 while every ``pytest`` run stayed green. This test runs the very same
:func:`scan` over the repo root, with the real allowlist, so the gate trips where it is
looked at — in the test output — rather than after a publish.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parent.parent
_CHECKER = _ROOT / "tools" / "check_public_tree.py"
_ALLOWLIST = _ROOT / "tools" / "public_tree_allowlist.txt"


def _load_checker() -> ModuleType:
    """Import the standalone script as a module (``tools/`` is not a package)."""
    spec = importlib.util.spec_from_file_location("check_public_tree", _CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_tree_has_no_forbidden_anchors() -> None:
    """Every tracked or untracked-not-ignored file is free of the personal anchors."""
    hits = _load_checker().scan(_ROOT, _ALLOWLIST)
    listing = "\n".join(f"{rel}:{lineno}: {pattern}" for rel, lineno, pattern in hits)
    assert not hits, f"forbidden-pattern hit(s) in the public tree:\n{listing}"
