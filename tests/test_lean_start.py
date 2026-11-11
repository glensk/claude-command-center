"""tp#1681 L7w: the per-job ``lean_start`` flag exports ``CC_LEAN_START=1`` at launch."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from command_center import accounts, cli
from command_center.store import Store


def test_lean_start_flag_adds_cc_lean_start() -> None:
    lean = accounts.session_env_flags(accounts.LaunchTarget("", lean_start=True))
    assert lean[accounts.LEAN_START_VAR] == "1"
    plain = accounts.session_env_flags(accounts.LaunchTarget(""))
    assert accounts.LEAN_START_VAR not in plain


def test_lean_start_pin_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(accounts.LEAN_START_VAR, raising=False)
    monkeypatch.setattr(accounts, "_mem_env", lambda _base: {})
    accounts.pin_environ("", False, "", lean_start=True)
    assert os.environ.get(accounts.LEAN_START_VAR) == "1"


def test_lean_start_column_round_trips(tmp_path: Path) -> None:
    with Store() as store:
        job = store.create_draft("sid-lean", str(tmp_path), "aim", lean_start=True)
        assert job.lean_start is True
        assert store.create_draft("sid-plain", str(tmp_path), "aim").lean_start is False
        assert accounts.LEAN_START_VAR in accounts.session_env_flags(job)


def test_new_job_lean_start_option(tmp_path: Path) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["new-job", "-a", "aim", "-c", str(tmp_path), "-l"])
    assert args.lean_start is True
    assert parser.parse_args(["new-job", "-a", "aim", "--lean-start"]).lean_start is True
    assert parser.parse_args(["new-job", "-a", "aim"]).lean_start is False
