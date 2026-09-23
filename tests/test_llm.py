"""The single routed entrance for ccc-owned LLM calls."""

from __future__ import annotations

import logging

import pytest

from command_center import config, llm


class _Proc:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_run_custom_feeds_stdin_and_labels_env(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd: str, **kw: object) -> _Proc:
        seen.update(cmd=cmd, **kw)
        return _Proc(0, "MODEL RESPONSE\n")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    assert llm.run_custom("PROMPT", "ai prompt", purpose="aim-score", note="#1") == (
        "MODEL RESPONSE"
    )
    assert seen["cmd"] == "ai prompt" and seen["input"] == "PROMPT"
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["CCC_LLM_PURPOSE"] == "aim-score" and env["CCC_LLM_NOTE"] == "#1"
    assert env["CCC_INTERNAL"] == "1" and env["AI_NO_AUTOCOMMIT"] == "1"


def test_run_model_uses_only_configured_router(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config, "load_config", lambda: config.Config(llm_custom_command="ai prompt -R judge")
    )
    seen: dict[str, str] = {}

    def fake_custom(prompt: str, command: str, **_kw: object) -> str:
        seen.update(prompt=prompt, command=command)
        return "ok"

    monkeypatch.setattr(llm, "run_custom", fake_custom)
    assert llm.run_model("P", "obsolete-model", purpose="aim-met") == "ok"
    assert seen == {"prompt": "P", "command": "ai prompt -R judge"}


def test_unset_router_fails_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(config, "load_config", lambda: config.Config(llm_custom_command=""))
    with caplog.at_level(logging.WARNING):
        assert llm.run_model("P", "ignored", purpose="short-aim") is None
    assert "router unavailable" in caplog.text and "short-aim" in caplog.text


def test_failed_router_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(config, "load_config", lambda: config.Config(llm_custom_command="router"))
    monkeypatch.setattr(llm, "run_custom", lambda *_a, **_k: None)
    with caplog.at_level(logging.WARNING):
        assert llm.run_model("P", "ignored", purpose="aim-score") is None
    assert "router failed" in caplog.text and "aim-score" in caplog.text


def test_run_custom_empty_or_nonzero_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    assert llm.run_custom("p", "   ") is None
    monkeypatch.setattr(llm.subprocess, "run", lambda *_a, **_k: _Proc(3, "partial"))
    assert llm.run_custom("p", "false") is None
