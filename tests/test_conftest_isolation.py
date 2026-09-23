"""Regression tests for the suite-wide env isolation in ``tests/conftest.py``."""

from __future__ import annotations

import os

from conftest import _is_ambient_claude_env


def test_ambient_claude_env_is_scrubbed() -> None:
    """No ambient ``CLAUDE_*`` / ``CODEX_IN_CLAUDE_*`` / ``CLAUDECODE`` reaches a test (tp#233)."""
    leaked = sorted(key for key in os.environ if _is_ambient_claude_env(key))
    assert not leaked, f"ambient Claude env leaked into the test: {leaked}"
    assert "CLAUDE_HOME" in os.environ  # pinned by _pin_claude_home, not scrubbed


def test_scrub_pattern() -> None:
    assert _is_ambient_claude_env("CLAUDE_PID")
    assert _is_ambient_claude_env("CLAUDE_SESSION_AIM")
    assert _is_ambient_claude_env("CLAUDECODE")
    assert _is_ambient_claude_env("CODEX_IN_CLAUDE_IGNORE_QUOTA")
    assert not _is_ambient_claude_env("CLAUDE_HOME")
    assert not _is_ambient_claude_env("CODEX_HOME")
    assert not _is_ambient_claude_env("HOME")
