"""Shared pieces of the fresh-interpreter "no heavy module" probes.

``test_lean_imports``, ``test_statusline_fast`` and ``test_entrypoint`` each run a probe
script in a child interpreter and inspect the ``sys.modules`` it reports. The forbidden
module list, its matcher, the child's ``PYTHONPATH`` and the ``entrypoint.main`` probe head
live here so the three cannot drift apart. Stdlib only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Modules the lean paths must never load. Matched exactly or as a dotted prefix by
# :func:`forbidden`, so ``command_center.usage`` does not catch ``usagecache``.
FORBIDDEN = (
    "command_center.cli",
    "command_center.config",
    "command_center.models",
    "command_center.store",
    "command_center.usage",
    "command_center.park",
    "command_center.tabsymbol",
    "command_center.accounts",
    "command_center.adapters",
    "dataclasses",
    "argparse",
    "rich",
    "textual",
)

# Runs ``entrypoint.main`` on ``sys.argv[2:]`` and leaves its exit status in ``code``;
# ``sys.argv[1]`` is the report path the caller's tail writes to.
ENTRYPOINT_PROBE_HEAD = """
import json, sys
from command_center import entrypoint
try:
    code = entrypoint.main(sys.argv[2:])
except SystemExit as exc:
    code = exc.code
"""


def forbidden(module: str) -> bool:
    """True when *module* is one of :data:`FORBIDDEN` or a submodule of one."""
    return any(module == name or module.startswith(name + ".") for name in FORBIDDEN)


def child_pythonpath(env: Mapping[str, str]) -> str:
    """ROOT prepended to *env*'s PYTHONPATH, so the conftest child guard stays on it."""
    return os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
