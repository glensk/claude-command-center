"""tp#1697 B14: router-skipped ccc LLM work goes on a pending queue, never counts a miss."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from command_center import llm, llm_pending


def _router_exit(monkeypatch: pytest.MonkeyPatch, rc: int) -> None:
    def run(cmd: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, rc, "", "skipped")

    monkeypatch.setattr(llm.subprocess, "run", run)


def test_exit_75_is_a_skip_consumed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _router_exit(monkeypatch, llm.EXIT_SKIPPED)
    assert llm.run_custom("p", "router", purpose="short-aim") is None
    assert llm.consume_skip("short-aim") is True
    assert llm.consume_skip("short-aim") is False


def test_other_failure_is_not_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    _router_exit(monkeypatch, 1)
    assert llm.run_custom("p", "router", purpose="voice-brief") is None
    assert llm.consume_skip("voice-brief") is False


def test_mark_clear_prune() -> None:
    llm_pending.mark("voice-brief", "s1", now=1000)
    llm_pending.mark("voice-brief", "s1", now=2000)  # the first skip's time is kept
    llm_pending.mark("short-aim", "s2", now=5000)
    assert llm_pending.load() == {"voice-brief": {"s1": 1000}, "short-aim": {"s2": 5000}}
    llm_pending.clear("short-aim", "s2")
    assert llm_pending.prune(now=1000 + llm_pending.MAX_AGE_SEC + 1) == {}
