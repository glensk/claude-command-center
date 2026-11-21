"""The tail-first rollout reader, its per-process memo and the aggregate cache key (tp#2513).

Parity: over awkward fixture rollouts the one-pass reader returns exactly what the old
whole-file readers returned (kept below as test-local reference implementations), at
several chunk sizes so lines straddle and exceed chunk boundaries. Cost: the reader
stops near the end of a big file and the memo opens each rollout once per process.
"""

# The usage <-> codex_in_claude import cycle predates this file (pylint reports it on the
# first module that imports both); it is not introduced here.
# pylint: disable=cyclic-import

from __future__ import annotations

import builtins
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from command_center import codex_in_claude as cic
from command_center import quota, usage


def _windowed(five_hour_pct: float) -> dict[str, Any]:
    """A healthy windowed block whose 5-hour window reads *five_hour_pct*."""
    return {
        "limit_id": "codex",
        "primary": {"used_percent": five_hour_pct, "window_minutes": 300, "resets_at": 1782320400},
        "secondary": {"used_percent": 45.0, "window_minutes": 10080, "resets_at": 1782893849},
    }


_WINDOWED = _windowed(12.0)
_PREMIUM_NULL = {"limit_id": "premium", "primary": None, "secondary": None}
_REFUSED = {
    "limit_id": "premium",
    "primary": None,
    "secondary": None,
    "rate_limit_reached_type": "workspace_owner_credits_depleted",
}
_MALFORMED = {
    "limit_id": "codex",
    "primary": {"used_percent": 3.0, "window_minutes": 0, "resets_at": 1782320400},
    "secondary": None,
}


# ── reference implementations: the pre-tp#2513 whole-file readers, verbatim ──────────
# pylint: disable=duplicate-code
def _ref_rate(path: Path) -> cic._CodexRateSnapshot | None:  # noqa: SLF001
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        fallback = int(path.stat().st_mtime)
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        rate_limits = cic._dig_rate_limits(obj)  # noqa: SLF001
        if rate_limits is None:
            continue
        windows, malformed = cic._windows_by_duration(rate_limits)  # noqa: SLF001
        if windows or malformed:
            return cic._CodexRateSnapshot(  # noqa: SLF001
                captured_at=cic._event_timestamp(obj, fallback),  # noqa: SLF001
                windows=windows,
                malformed=malformed,
            )
    return None


def _ref_block(path: Path) -> cic._CodexRefusal | None:  # noqa: SLF001
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        fallback = int(path.stat().st_mtime)
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        rate_limits = cic._dig_rate_limits(obj)  # noqa: SLF001
        if rate_limits is None:
            continue
        raw = rate_limits.get("rate_limit_reached_type")
        return cic._CodexRefusal(  # noqa: SLF001
            captured_at=cic._event_timestamp(obj, fallback),  # noqa: SLF001
            reached_type=raw.strip() if isinstance(raw, str) else "",
        )
    return None


# pylint: enable=duplicate-code


# ── fixtures ─────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _fresh_memo() -> Iterator[None]:
    """Every test starts (and ends) with an empty per-process rollout memo."""
    cic._rollout_memo.clear()  # noqa: SLF001
    yield
    cic._rollout_memo.clear()  # noqa: SLF001


def _event(rate_limits: dict | None, ts: str | None = None) -> str:
    obj: dict[str, Any] = {
        "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": rate_limits},
    }
    if ts is not None:
        obj["timestamp"] = ts
    return json.dumps(obj)


def _filler(n: int = 1, size: int = 40) -> list[str]:
    return [json.dumps({"type": "response_item", "text": "x" * size}) for _ in range(n)]


def _rollout(home: Path, name: str, data: bytes, mtime: int | None = None) -> Path:
    day = home / "sessions" / "2026" / "10" / "26"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-10-26T10-00-00-{name}.jsonl"
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _lines(*lines: str, newline: str = "\n", trailing: bool = True) -> bytes:
    body = newline.join(lines) + (newline if trailing else "")
    return body.encode("utf-8")


_LONG = "y" * 700  # longer than the small chunk sizes below
_CASES: dict[str, bytes] = {
    "windowless_premium_newest": _lines(
        *_filler(3),
        _event(_WINDOWED, "2026-10-26T09:00:00Z"),
        *_filler(2),
        _event(_PREMIUM_NULL, "2026-10-26T09:05:00Z"),
        *_filler(1),
    ),
    "malformed_window_newest": _lines(
        _event(_WINDOWED, "2026-10-26T09:00:00Z"),
        _event(_MALFORMED, "2026-10-26T09:10:00Z"),
    ),
    "refusal_newest": _lines(
        _event(_WINDOWED, "2026-10-26T09:00:00Z"),
        *_filler(4),
        _event(_REFUSED, "2026-10-26T09:20:00Z"),
    ),
    "refusal_superseded": _lines(
        _event(_REFUSED, "2026-10-26T09:00:00Z"),
        *_filler(2),
        _event(_WINDOWED, "2026-10-26T09:30:00Z"),
    ),
    "block_in_first_line": _lines(_event(_WINDOWED), *_filler(6)),
    "no_trailing_newline": _lines(
        *_filler(2), _event(_PREMIUM_NULL), _event(_WINDOWED, "1782300000"), trailing=False
    ),
    "crlf": _lines(
        *_filler(2),
        _event(_WINDOWED, "2026-10-26T09:00:00Z"),
        _event(_REFUSED),
        *_filler(1),
        newline="\r\n",
    ),
    "line_longer_than_chunk": _lines(
        _event(_WINDOWED, "2026-10-26T08:00:00Z"),
        json.dumps({"type": "response_item", "text": _LONG * 5}),
        json.dumps({"type": "x", "pad": _LONG, "payload": {"rate_limits": _PREMIUM_NULL}}),
        *_filler(1),
    ),
    "invalid_utf8": (
        _lines(_event(_WINDOWED, "2026-10-26T07:00:00Z"))
        + b'{"type":"x","text":"\xff\xfe\xc3"}\n'
        + _event(_REFUSED).encode().replace(b'"premium"', b'"pr\xffemium"')
        + b"\n"
        + b"\xe2\x82\n"
    ),
    "exotic_line_breaks": _lines(
        _event(_WINDOWED, "2026-10-26T06:00:00Z"),
        _event(_REFUSED).replace('"premium"', '"pre\\u2028mium"') + " " + _filler()[0],
        "\x0c" + _event(_MALFORMED),
    ),
    "garbage_rate_limits_lines": _lines(
        _event(_WINDOWED, "2026-10-26T05:00:00Z"),
        '{"rate_limits": broken json',
        json.dumps({"rate_limits": "not-a-dict"}),
        json.dumps({"payload": {"rate_limits": None}}),
    ),
    "no_blocks": _lines(*_filler(5)),
    "empty": b"",
}


@pytest.mark.parametrize(
    "chunks", [(7, 7), (64, 64), (333, 333), (5, 4096), (16 * 1024, 256 * 1024)]
)
@pytest.mark.parametrize("case", sorted(_CASES))
def test_tail_reader_matches_full_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, chunks: tuple[int, int]
) -> None:
    """``chunks`` = (first, max) chunk size; small ones put lines across boundaries."""
    monkeypatch.setattr(cic, "_ROLLOUT_FIRST_CHUNK", chunks[0])
    monkeypatch.setattr(cic, "_ROLLOUT_CHUNK", chunks[1])
    path = _rollout(tmp_path / "home", case, _CASES[case], mtime=1782000000)
    expected = (_ref_rate(path), _ref_block(path))
    assert cic._rollout_tail_blocks(path) == expected  # noqa: SLF001
    assert cic._latest_rate_limits_event(path) == expected[0]  # noqa: SLF001
    assert cic._latest_limit_block(path) == expected[1]  # noqa: SLF001


def test_cases_exercise_both_answers() -> None:
    """Guard the fixture set itself: refusals, windows and malformed all occur."""
    assert b"workspace_owner_credits_depleted" in _CASES["refusal_newest"]
    assert b'"window_minutes": 0' in _CASES["malformed_window_newest"]


def test_missing_file_is_no_evidence(tmp_path: Path) -> None:
    missing = tmp_path / "rollout-missing.jsonl"
    assert cic._rollout_tail_blocks(missing) == (None, None)  # noqa: SLF001
    assert cic._latest_rate_limits_event(missing) is None  # noqa: SLF001
    assert cic._latest_limit_block(missing) is None  # noqa: SLF001


def test_newest_event_not_in_newest_mtime_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _rollout(
        home,
        "touched-late",
        _lines(_event(_WINDOWED, "2026-10-20T09:00:00Z"), _event(_REFUSED, "2026-10-20T09:01:00Z")),
        mtime=1782009000,
    )
    newer = _windowed(81.0)
    _rollout(
        home,
        "really-newer",
        _lines(_event(_REFUSED, "2026-10-25T09:00:00Z"), _event(newer, "2026-10-25T10:00:00Z")),
        mtime=1782001000,
    )
    files = sorted(
        (home / "sessions").glob("**/rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    ref_rates = [r for p in files if (r := _ref_rate(p)) is not None]
    ref_blocks = [b for p in files if (b := _ref_block(p)) is not None]
    want_rate = max(ref_rates, key=lambda item: item.captured_at)
    want_block = max(ref_blocks, key=lambda item: item.captured_at)
    assert cic._codex_rate_snapshot(home) == want_rate  # noqa: SLF001
    assert want_rate.windows[300].used_percent == 81.0
    # The newest block overall is the healthy one: the older refusal is superseded.
    assert want_block.reached_type == ""
    assert cic.codex_refusal(home) is None
    snap = usage._codex_rollout_snapshot(files, 0)  # noqa: SLF001
    assert snap is not None
    assert snap.five_hour is not None
    assert snap.five_hour.used_percentage == 81.0


# ── I/O accounting ───────────────────────────────────────────────────────────────────
class _CountingFile:
    """A binary file proxy that counts bytes read and can run a hook on first read."""

    def __init__(self, handle: Any, ledger: dict[str, Any], hook: Callable[[], None] | None):
        self._handle = handle
        self._ledger = ledger
        self._hook = hook

    def __enter__(self) -> _CountingFile:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._handle.close()

    def fileno(self) -> int:
        return int(self._handle.fileno())

    def seek(self, pos: int) -> int:
        return int(self._handle.seek(pos))

    def read(self, size: int = -1) -> bytes:
        if self._hook is not None:
            hook, self._hook = self._hook, None
            hook()
        data: bytes = self._handle.read(size)
        self._ledger["bytes"] += len(data)
        return data


def _count_opens(
    monkeypatch: pytest.MonkeyPatch, on_open: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Wrap ``codex_in_claude.open``: count rollout opens per path and bytes read."""
    ledger: dict[str, Any] = {"bytes": 0, "opens": {}}
    real_open = builtins.open

    def counting_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(file, *args, **kwargs)  # pylint: disable=consider-using-with
        name = os.fspath(file)
        if "rollout-" not in os.path.basename(name):
            return handle
        ledger["opens"][name] = ledger["opens"].get(name, 0) + 1
        hook = (lambda: on_open(name)) if on_open is not None else None
        return _CountingFile(handle, ledger, hook)

    monkeypatch.setattr(cic, "open", counting_open, raising=False)
    return ledger


def test_big_file_is_answered_from_its_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filler = (json.dumps({"type": "response_item", "text": "z" * 1000}) + "\n").encode()
    tail = _lines(
        _event(_WINDOWED, "2026-10-26T09:00:00Z"), _event(_REFUSED, "2026-10-26T09:01:00Z")
    )
    assert len(tail) < 4096
    body = filler * ((20 * 1024 * 1024) // len(filler)) + tail
    path = _rollout(tmp_path / "home", "big", body)
    ledger = _count_opens(monkeypatch)
    rate, refusal = cic._rollout_tail_blocks(path)  # noqa: SLF001
    assert rate is not None
    assert refusal is not None
    assert refusal.reached_type == "workspace_owner_credits_depleted"
    assert 0 < ledger["bytes"] <= 2 * cic._ROLLOUT_CHUNK  # noqa: SLF001


def _seat_rollouts(fixture_seats: dict[str, Path]) -> list[Path]:
    paths = []
    for label, home in fixture_seats.items():
        paths.append(
            _rollout(home, f"{label}-old", _lines(_event(_WINDOWED, "2026-10-20T09:00:00Z")))
        )
        paths.append(
            _rollout(
                home,
                f"{label}-new",
                _lines(_event(_PREMIUM_NULL, "2026-10-26T09:00:00Z"), *_filler(2)),
            )
        )
    return paths


def test_quota_reads_each_rollout_once_per_process(
    three_seats: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _seat_rollouts(three_seats.seats)
    ledger = _count_opens(monkeypatch)
    now = 1782000000
    for _ in range(7):
        quota._codex_quotas(now, {})  # noqa: SLF001
    opened = {os.path.realpath(name): n for name, n in ledger["opens"].items()}
    assert opened == {os.path.realpath(p): 1 for p in paths}

    # A live-cache rewrite misses the aggregate cache but re-opens no rollout.
    for home in three_seats.seats.values():
        live = usage._codex_usage_path(home)  # noqa: SLF001
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text(json.dumps({"captured_at": 1}), encoding="utf-8")
    quota._codex_quotas(now, {})  # noqa: SLF001
    for home in three_seats.seats.values():
        usage._codex_usage_path(home).write_text(  # noqa: SLF001
            json.dumps({"captured_at": 22}), encoding="utf-8"
        )
    quota._codex_quotas(now, {})  # noqa: SLF001
    assert sum(ledger["opens"].values()) == len(paths)


def test_touch_and_inode_replacement_are_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _rollout(tmp_path / "home", "a", _lines(_event(_WINDOWED)), mtime=1782000000)
    ledger = _count_opens(monkeypatch)
    first = cic._latest_rate_limits_event(path)  # noqa: SLF001
    assert cic._latest_rate_limits_event(path) == first  # noqa: SLF001
    assert sum(ledger["opens"].values()) == 1

    os.utime(path, (1782000100, 1782000100))  # mtime moves
    cic._latest_rate_limits_event(path)  # noqa: SLF001
    assert sum(ledger["opens"].values()) == 2

    with path.open("ab") as handle:  # size moves
        handle.write(_lines(_event(_REFUSED)))
    assert cic._latest_limit_block(path) is not None  # noqa: SLF001
    assert sum(ledger["opens"].values()) == 3

    # Same size, same mtime_ns, new inode: only st_ino tells them apart.
    st = path.stat()
    replacement = path.read_bytes().replace(b"12.0", b"99.0")
    assert b"99.0" in replacement
    assert len(replacement) == st.st_size
    tmp = path.with_name("replacement.tmp")
    tmp.write_bytes(replacement)
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, path)
    assert path.stat().st_ino != st.st_ino
    assert path.stat().st_mtime_ns == st.st_mtime_ns
    rate = cic._latest_rate_limits_event(path)  # noqa: SLF001
    assert rate is not None
    assert rate.windows[300].used_percent == 99.0
    assert sum(ledger["opens"].values()) == 4


def test_append_during_read_is_not_cached_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _rollout(tmp_path / "home", "grow", _lines(_event(_WINDOWED)))
    appended: list[str] = []

    def append_once(name: str) -> None:
        if appended:
            return
        appended.append(name)
        with builtins.open(name, "ab") as handle:
            handle.write(_lines(_event(_REFUSED)))

    ledger = _count_opens(monkeypatch, append_once)
    refusal = cic._latest_limit_block(path)  # noqa: SLF001
    # The first pass read a stale fingerprint; the retry saw the append and was stored.
    assert sum(ledger["opens"].values()) == 2
    assert refusal is not None
    assert refusal.reached_type == "workspace_owner_credits_depleted"
    canonical = os.path.realpath(path)
    stored = cic._rollout_memo[cic._rollout_partition(canonical)][canonical]  # noqa: SLF001
    assert stored[0] == cic._rollout_fingerprint(path.stat())  # noqa: SLF001


def test_append_on_every_read_is_returned_uncached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _rollout(tmp_path / "home", "busy", _lines(_event(_WINDOWED)))

    def append_always(name: str) -> None:
        with builtins.open(name, "ab") as handle:
            handle.write(_lines(*_filler()))

    ledger = _count_opens(monkeypatch, append_always)
    assert cic._latest_rate_limits_event(path) is not None  # noqa: SLF001
    assert sum(ledger["opens"].values()) == 2  # one retry, then give up caching
    canonical = os.path.realpath(path)
    partition = cic._rollout_partition(canonical)  # noqa: SLF001
    assert canonical not in cic._rollout_memo.get(partition, {})  # noqa: SLF001


def _memo_names(home: Path) -> set[str]:
    partition = cic._rollout_partition(  # noqa: SLF001
        os.path.realpath(home / "sessions" / "x" / "rollout-x.jsonl")
    )
    return {os.path.basename(name) for name in cic._rollout_memo.get(partition, {})}  # noqa: SLF001


def test_memo_is_pruned_to_the_scan_set_per_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cic, "_CODEX_SCAN_LIMIT", 2)
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    a1 = _rollout(home_a, "a1", _lines(_event(_WINDOWED)), mtime=1782000001)
    _rollout(home_a, "a2", _lines(_event(_WINDOWED)), mtime=1782000002)
    _rollout(home_a, "a3", _lines(_event(_WINDOWED)), mtime=1782000003)
    _rollout(home_b, "b1", _lines(_event(_REFUSED)), mtime=1782000001)
    cic.codex_refusal(home_a)
    cic.codex_refusal(home_b)
    assert _memo_names(home_a) == {
        "rollout-2026-10-26T10-00-00-a2.jsonl",
        "rollout-2026-10-26T10-00-00-a3.jsonl",
    }
    assert _memo_names(home_b) == {"rollout-2026-10-26T10-00-00-b1.jsonl"}

    os.utime(a1, (1782000009, 1782000009))  # a1 becomes newest: a2 leaves the scan set
    cic._codex_rate_snapshot(home_a)  # noqa: SLF001
    assert _memo_names(home_a) == {
        "rollout-2026-10-26T10-00-00-a1.jsonl",
        "rollout-2026-10-26T10-00-00-a3.jsonl",
    }
    assert _memo_names(home_b) == {"rollout-2026-10-26T10-00-00-b1.jsonl"}


def test_aggregate_key_sees_a_change_below_the_newest_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _rollout(home, "newest", _lines(_event(_PREMIUM_NULL)), mtime=1782002000)
    second = _rollout(
        home, "second", _lines(_event(_WINDOWED, "2026-10-20T09:00:00Z")), mtime=1782001000
    )
    now = 1782000000
    before = usage.read_codex_usage(now, home)
    assert before is not None
    assert before.five_hour is not None
    assert before.five_hour.used_percentage == 12.0

    newer = _windowed(64.0)
    with second.open("ab") as handle:
        handle.write(_lines(_event(newer, "2026-10-21T09:00:00Z")))
    os.utime(second, (1782001000, 1782001000))  # still second-newest; newest untouched
    after = usage.read_codex_usage(now, home)
    assert after is not None
    assert after.five_hour is not None
    assert after.five_hour.used_percentage == 64.0
