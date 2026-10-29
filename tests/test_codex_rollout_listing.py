"""The cached rollout listing equals a fresh glob through add / remove / append."""

from __future__ import annotations

import os
from pathlib import Path

from command_center import usage


def _fresh(home: Path) -> list[Path]:
    return sorted(
        (home / "sessions").glob("**/rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _touch(path: Path, mtime: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_listing_tracks_adds_removes_and_appends(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    day1 = home / "sessions" / "2026" / "10" / "28"
    day2 = home / "sessions" / "2026" / "10" / "29"
    _touch(day1 / "rollout-a.jsonl", 1000)
    _touch(day1 / "notes.txt", 5000)
    _touch(day2 / "rollout-b.jsonl", 2000)
    assert usage._codex_rollout_files(home) == _fresh(home)

    _touch(day2 / "rollout-c.jsonl", 3000)  # new file in a known directory
    assert usage._codex_rollout_files(home) == _fresh(home)

    _touch(home / "sessions" / "2026" / "11" / "01" / "rollout-d.jsonl", 4000)  # new month
    assert usage._codex_rollout_files(home) == _fresh(home)

    os.utime(day1 / "rollout-a.jsonl", (9000, 9000))  # append: file mtime moves, dir's does not
    got = usage._codex_rollout_files(home)
    assert got == _fresh(home)
    assert got[0].name == "rollout-a.jsonl"

    (day2 / "rollout-b.jsonl").unlink()
    (home / "sessions" / "2026" / "11" / "01" / "rollout-d.jsonl").unlink()
    (home / "sessions" / "2026" / "11" / "01").rmdir()
    assert usage._codex_rollout_files(home) == _fresh(home)


def test_missing_home_is_empty(tmp_path: Path) -> None:
    assert usage._codex_rollout_files(tmp_path / "nope") == []
