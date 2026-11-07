"""Tests-only child guard (tp#1793 B3): loaded by every Python child of the ccc test suite.

``tests/conftest.py`` prepends this directory to ``PYTHONPATH`` and pins
``CCC_TEST_KILL_AUDIT`` / ``CCC_TEST_ROOT_PID`` in the test env, so a child such as
``sys.executable -m command_center …`` imports this module at start-up (``site``). It
installs the same kill guard, Popen guard and audit writer as conftest, from the shared
:mod:`ccc_test_guard`. Without ``CCC_TEST_KILL_AUDIT`` it does nothing.

This file shadows any other ``sitecustomize`` further down ``sys.path`` (a venv's or the
interpreter's), so that one is chain-imported afterwards, as ``site`` would have imported it.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys

import ccc_test_guard

_HERE = os.path.dirname(os.path.realpath(__file__))
_CHAINED = "_ccc_test_chained_sitecustomize"


def _chain_other_sitecustomize() -> None:
    """Import the next ``sitecustomize`` on ``sys.path`` (not this one), if there is one."""
    others = [entry for entry in sys.path if os.path.realpath(entry or os.curdir) != _HERE]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", others)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[_CHAINED] = module
    spec.loader.exec_module(module)


ccc_test_guard.install_child_guard()
_chain_other_sitecustomize()
