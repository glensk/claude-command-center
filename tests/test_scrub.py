"""Unit tests for :mod:`command_center.scrub` — the fail-closed scrubber contract.

Hermetic: every "scrubber" is an executable shell stub from :mod:`scrubstub`; no real
broker is ever contacted (a developer machine with one on PATH must not change a result).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scrubstub import DUMMY_VALUE, PLACEHOLDER, stub_scrubber

from command_center import scrub

DOC = f'---\nccc_mirror: "session"\nsession_id: "abc"\n---\n\nhello {DUMMY_VALUE}\n'


def _scrubber(tmp_path: Path, mode: str = "redact", *, verb: str = "scrub") -> scrub.Scrubber:
    stub = stub_scrubber(tmp_path, mode)
    resolution = scrub.resolve_scrubber(stub.scrub_cmd, verb=verb)
    assert resolution.ok and resolution.scrubber is not None, resolution.reason
    return resolution.scrubber


# ------------------------------ resolution ------------------------------ #
def test_resolve_reasons_never_raise(tmp_path: Path) -> None:
    assert "no scrubber configured" in scrub.resolve_scrubber("").reason
    assert "no scrubber configured" in scrub.resolve_scrubber("   ").reason
    assert "not parseable" in scrub.resolve_scrubber('"unterminated scrub').reason
    assert "does not exist" in scrub.resolve_scrubber("/nonexistent/x scrub").reason
    plain = tmp_path / "plain.sh"
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    plain.chmod(0o644)
    assert "not executable" in scrub.resolve_scrubber(f"{plain} scrub").reason
    nohelp = stub_scrubber(tmp_path, "nohelp")
    assert "'scrub' subcommand" in scrub.resolve_scrubber(nohelp.scrub_cmd).reason
    for policy in ("", "/nonexistent/x scrub", nohelp.scrub_cmd):
        resolution = scrub.resolve_scrubber(policy)
        assert not resolution.ok and resolution.scrubber is None


def test_resolve_verbatim_path_keeps_arguments_and_policy(tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path)
    policy = f"{stub.path} scrub --shapes"
    resolution = scrub.resolve_scrubber(policy)
    assert resolution.ok and resolution.scrubber is not None
    assert resolution.scrubber.argv == (str(stub.path), "scrub", "--shapes")
    assert resolution.scrubber.policy == policy
    assert resolution.scrubber.executable == str(stub.path)
    # The check verb replaces the configured tail: `<exe> check`.
    checker = scrub.resolve_scrubber(policy, verb="check")
    assert checker.ok and checker.scrubber is not None
    assert checker.scrubber.argv == (str(stub.path), "check")


def test_resolve_bare_name_via_env_override_then_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = stub_scrubber(tmp_path)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.delenv("SECRET_BROKER_CLIENT", raising=False)
    monkeypatch.setenv("PATH", str(empty))
    assert "Missing dependency" in scrub.resolve_scrubber("fake-broker scrub").reason

    monkeypatch.setenv("SECRET_BROKER_CLIENT", str(stub.path))
    via_env = scrub.resolve_scrubber("fake-broker scrub")
    assert via_env.ok and via_env.scrubber is not None
    assert via_env.scrubber.executable == str(stub.path)

    monkeypatch.delenv("SECRET_BROKER_CLIENT")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "fake-broker").symlink_to(stub.path)
    monkeypatch.setenv("PATH", str(bindir))
    via_path = scrub.resolve_scrubber("fake-broker scrub")
    assert via_path.ok and via_path.scrubber is not None
    assert Path(via_path.scrubber.executable).resolve() == stub.path.resolve()


# ------------------------------ scrub ------------------------------ #
def test_scrub_returns_vouched_text_and_labels(tmp_path: Path) -> None:
    scrubber = _scrubber(tmp_path)
    result = scrub.scrub(scrubber, DOC)
    assert result.ok and not result.withheld and result.reason == ""
    assert PLACEHOLDER in result.text and DUMMY_VALUE not in result.text
    assert result.labels == ("test.key",)
    clean = scrub.scrub(scrubber, "nothing here\n")
    assert clean.ok and clean.text == "nothing here\n" and not clean.labels


@pytest.mark.parametrize(
    "mode, needle",
    [
        ("exit3", "exit 3"),
        ("empty", "no output"),
        ("oversize", "grew by"),
        ("badutf8", "not valid UTF-8"),
    ],
)
def test_scrub_withholds_on_a_broken_contract(tmp_path: Path, mode: str, needle: str) -> None:
    result = scrub.scrub(_scrubber(tmp_path, mode), DOC)
    assert result.withheld and result.text == "" and not result.labels
    assert needle in result.reason and DUMMY_VALUE not in result.reason


def test_scrub_timeout_is_withheld(tmp_path: Path) -> None:
    result = scrub.scrub(_scrubber(tmp_path, "sleep"), DOC, timeout=0.3)
    assert result.withheld and "timed out" in result.reason


def test_scrub_spawn_failure_is_withheld() -> None:
    ghost = scrub.Scrubber(argv=("/nonexistent/fake-broker", "scrub"), policy="x")
    result = scrub.scrub(ghost, DOC)
    assert result.withheld and "could not be started" in result.reason


# ------------------------------ check ------------------------------ #
def test_check_classifies_the_v1_verdicts(tmp_path: Path) -> None:
    checker = _scrubber(tmp_path, "check", verb="check")
    assert scrub.check(checker, "plain text").state == scrub.CLEAN
    leak = scrub.check(checker, f"token {DUMMY_VALUE}")
    assert leak.state == scrub.LEAK and leak.labels == ("test.key",)

    degraded = scrub.check(_scrubber(tmp_path, "exit3", verb="check"), "x")
    assert degraded.state == scrub.DEGRADED and "exit 3" in degraded.reason

    crashed = scrub.check(_scrubber(tmp_path, "checkcrash", verb="check"), "x")
    assert crashed.state == scrub.DEGRADED and "without a v1 verdict marker" in crashed.reason


def test_sha256_is_a_stable_identity() -> None:
    assert scrub.sha256("a") == scrub.sha256("a")
    assert scrub.sha256("a") != scrub.sha256("b")
