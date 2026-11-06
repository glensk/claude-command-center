"""Tests for the compact git-status helper and its fair scheduler (tp#1758)."""

# pylint: disable=redefined-outer-name  # pytest fixtures are injected by parameter name

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from command_center import gitstatus


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture(autouse=True)
def _fresh_scheduler() -> Iterator[None]:
    gitstatus.reset()
    yield
    gitstatus.reset()


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the git call with a recorder answering ✓ (no real process)."""
    calls: list[str] = []

    def _fake(cwd: str) -> tuple[str, str]:
        calls.append(cwd)
        return ("✓", "green")

    monkeypatch.setattr(gitstatus, "_compute", _fake)
    return calls


def test_short_non_repo_and_states(tmp_path: Path) -> None:
    """Real git: blank for a non-repo, ✓ clean, ● dirty — each through refresh_one."""
    t = 1000.0
    assert gitstatus.refresh_one([str(tmp_path)], now=t) == str(tmp_path)
    assert gitstatus.cached(str(tmp_path)) == ("", "grey42")
    assert gitstatus.cached("") == ("", "grey42")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    t += 3
    assert gitstatus.refresh_one([str(repo)], now=t) == str(repo)
    assert gitstatus.cached(str(repo)) == ("✓", "green")

    (repo / "a.txt").write_text("changed", encoding="utf-8")
    t += 30
    assert gitstatus.refresh_one([str(repo)], now=t) == str(repo)
    assert gitstatus.cached(str(repo)) == ("●", "#ff8800")


def test_cached_never_spawns_git(runs: list[str]) -> None:
    """Cold start / first paint: cached() is blank, runs nothing; the first refresh is at once."""
    assert gitstatus.cached("/r/a") == ("", "grey42")
    assert runs == []
    assert gitstatus.refresh_one(["/r/a", "/r/b"], now=0.0) == "/r/a"
    assert runs == ["/r/a"]
    assert gitstatus.cached("/r/a") == ("✓", "green")
    assert gitstatus.cached("/r/b") == ("", "grey42")  # not its turn yet, still blank


def test_spacing_at_most_one_run_per_three_seconds(runs: list[str]) -> None:
    cwds = [f"/r/{i}" for i in range(5)]
    assert gitstatus.refresh_one(cwds, now=100.0) == "/r/0"
    for dt in (0.0, 0.5, 1.0, 2.9):
        assert gitstatus.refresh_one(cwds, now=100.0 + dt) is None
    assert gitstatus.refresh_one(cwds, now=103.0) == "/r/1"
    assert runs == ["/r/0", "/r/1"]
    # Ticks every 0.5 s for a minute: never more than one run per 3 s window.
    times = [103.0 + 0.5 * k for k in range(1, 121)]
    ran_at = [t for t in times if gitstatus.refresh_one(cwds, now=t) is not None]
    assert all(b - a >= 3.0 for a, b in zip(ran_at, ran_at[1:], strict=False))


def test_fairness_over_eighteen_cwds(runs: list[str]) -> None:
    """Every cwd gets its turn, oldest first; no cwd runs twice before all ran once."""
    cwds = [f"/repo/{i:02d}" for i in range(18)]
    t = 0.0
    for _ in range(18):
        assert gitstatus.refresh_one(cwds, now=t) is not None
        t += 3.0
    assert runs == cwds  # cold: display order
    # Next round: still oldest-first, each one again exactly once.
    for _ in range(18):
        assert gitstatus.refresh_one(cwds, now=t) is not None
        t += 3.0
    assert runs[18:] == cwds
    # A reordered list does not change who is most overdue.
    assert gitstatus.refresh_one(list(reversed(cwds)), now=t) == cwds[0]


def test_recently_refreshed_cwd_is_not_eligible(runs: list[str]) -> None:
    """A lone cwd is re-run only after the old 20 s TTL, not every 3 s."""
    assert gitstatus.refresh_one(["/r/a"], now=0.0) == "/r/a"
    assert gitstatus.refresh_one(["/r/a"], now=3.0) is None
    assert gitstatus.refresh_one(["/r/a"], now=19.9) is None
    assert gitstatus.refresh_one(["/r/a"], now=20.0) == "/r/a"
    assert runs == ["/r/a", "/r/a"]


def test_duplicate_and_blank_cwds(runs: list[str]) -> None:
    cwds = ["", "/r/a", "/r/a", "", "/r/b", "/r/a"]
    assert gitstatus.refresh_one(cwds, now=0.0) == "/r/a"
    assert gitstatus.refresh_one(cwds, now=3.0) == "/r/b"
    assert gitstatus.refresh_one(cwds, now=6.0) is None  # both fresh: nothing to do
    assert runs == ["/r/a", "/r/b"]
    assert gitstatus.refresh_one(["", ""], now=100.0) is None


def test_git_failure_keeps_stale_value_and_takes_its_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed git run (timeout, no git) keeps the last glyph and does not starve others."""
    answers: dict[str, tuple[str, str] | None] = {"/r/a": ("●", "#ff8800"), "/r/b": ("✓", "green")}
    calls: list[str] = []

    def _fake(cwd: str) -> tuple[str, str] | None:
        calls.append(cwd)
        return answers[cwd]

    monkeypatch.setattr(gitstatus, "_compute", _fake)
    gitstatus.refresh_one(["/r/a", "/r/b"], now=0.0)
    gitstatus.refresh_one(["/r/a", "/r/b"], now=3.0)
    answers["/r/a"] = None  # git now fails for /r/a
    assert gitstatus.refresh_one(["/r/a", "/r/b"], now=30.0) == "/r/a"
    assert gitstatus.cached("/r/a") == ("●", "#ff8800")  # stale value stays displayed
    assert gitstatus.refresh_one(["/r/a", "/r/b"], now=33.0) == "/r/b"
    assert calls == ["/r/a", "/r/b", "/r/a", "/r/b"]


def test_compute_reports_a_failed_spawn_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(gitstatus.subprocess, "run", _boom)
    assert gitstatus._compute("/r/a") is None
