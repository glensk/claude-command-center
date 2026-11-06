"""`ccc keep` says what it did: `--off` clears the flag and must not print "keep set" (tp#1785)."""

from __future__ import annotations

import argparse

import pytest

from command_center import cli
from command_center.store import Store

SID = "11111111-2222-3333-4444-555555555555"


def _keep(off: bool, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(cli, "_adapter", lambda: None)
    monkeypatch.setattr(cli, "resolve_session_id", lambda _a, given, _c: given)
    return cli.cmd_keep(argparse.Namespace(session=SID, off=off))


def test_keep_on_says_set(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    assert _keep(False, monkeypatch) == 0
    assert capsys.readouterr().out.strip() == f"keep set for {SID}"
    with Store() as store:
        row = store.get(SID)
    assert row is not None and row.keep


def test_keep_off_says_cleared(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _keep(False, monkeypatch)
    capsys.readouterr()
    assert _keep(True, monkeypatch) == 0
    out = capsys.readouterr().out.strip()
    assert out == f"keep cleared for {SID}"
    assert "keep set" not in out
    with Store() as store:
        row = store.get(SID)
    assert row is not None and not row.keep
