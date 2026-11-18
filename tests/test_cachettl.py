"""Unit tests for the prompt-cache TTL countdown helper (:mod:`command_center.cachettl`).

Covers the colour boundaries (1200 green / 600 orange), the ``❄ cold`` expiry, the
``M:SS`` formatting, a missing transcript (empty cell), the ``CC_CACHE_TTL_S`` override
incl. junk-value fallback, and — the point of the anchor rework — that the countdown
clocks off the newest main-chain ``"type":"assistant"`` transcript entry (its last real
API turn) rather than the transcript's raw mtime.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from command_center import accounts, cachettl
from command_center.models import Session


@pytest.fixture(autouse=True)
def _clear_anchor_cache() -> Iterator[None]:
    """Isolate the module-level anchor read-cache between tests."""
    cachettl._ANCHOR_CACHE.clear()
    yield
    cachettl._ANCHOR_CACHE.clear()


def _cd(remaining: int, ttl: int = 3600) -> tuple[str, str]:
    """``countdown`` for an exact number of *remaining* seconds under *ttl* (anchor=0)."""
    return cast(
        "tuple[str, str]", cachettl.countdown(anchor=0.0, now=float(ttl - remaining), ttl=ttl)
    )


# --------------------------------------------------------------------------- boundaries
def test_countdown_green_at_and_above_1200() -> None:
    text, level = _cd(1200)
    assert level == "green"
    assert text == "♨ 20:00"
    assert _cd(3599)[1] == "green"


def test_countdown_orange_between_600_and_1200() -> None:
    assert _cd(1199) == ("♨ 19:59", "orange")
    assert _cd(600) == ("♨ 10:00", "orange")


def test_countdown_red_below_600_while_warm() -> None:
    assert _cd(599) == ("♨ 9:59", "red")
    assert _cd(1) == ("♨ 0:01", "red")


def test_countdown_cold_at_zero_and_negative() -> None:
    assert _cd(0) == ("❄ cold", "red")
    assert _cd(-5) == ("❄ cold", "red")


# --------------------------------------------------------------------------- formatting
def test_countdown_mmss_formatting() -> None:
    # Whole minutes (no zero-pad) + zero-padded seconds.
    assert _cd(3587)[0] == "♨ 59:47"
    assert _cd(187)[0] == "♨ 3:07"


# --------------------------------------------------------------------------- empty cell
def test_countdown_missing_transcript_is_empty() -> None:
    assert cachettl.countdown(None, 123.0) == ("", "")


# --------------------------------------------------------------------------- TTL / env
def test_cache_ttl_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    assert cachettl.cache_ttl_seconds() == 3600


def test_cache_ttl_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CC_CACHE_TTL_S", "1800")
    assert cachettl.cache_ttl_seconds() == 1800
    # With a 1800 s TTL, 600 s elapsed leaves 1200 s → green (the env drives countdown too).
    assert cachettl.countdown(anchor=0.0, now=600.0) == ("♨ 20:00", "green")


@pytest.mark.parametrize("junk", ["notanumber", "", "1800.5", "0", "-5"])
def test_cache_ttl_junk_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, junk: str) -> None:
    monkeypatch.setenv("CC_CACHE_TTL_S", junk)
    assert cachettl.cache_ttl_seconds() == 3600


# --------------------------------------------------------------------------- transcript builders
def _iso(epoch: float) -> str:
    """UTC ISO-8601 with a trailing ``Z`` — the shape Claude Code writes on every entry."""
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch))
    micros = round((epoch - int(epoch)) * 1_000_000)
    return f"{base}.{micros:06d}Z"


def _assistant(
    epoch: float,
    *,
    sidechain: bool = False,
    cache_read: int = 150_000,
    model: str = "claude-opus-4-5",
    error: bool = False,
) -> str:
    """A compact main-chain (or ``isSidechain``) assistant transcript line — one API turn.

    By default a real cache request (it reads *cache_read* cached tokens); ``cache_read=0``
    with ``model="<synthetic>"`` and ``error=True`` is the usage-limit refusal's shape.
    """
    row: dict[str, object] = {
        "type": "assistant",
        "isSidechain": sidechain,
        "timestamp": _iso(epoch),
        "requestId": "req_abc",
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": 3,
                "output_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cache_read,
            },
            "content": [{"type": "text", "text": "ok"}],
        },
    }
    if error:
        row["isApiErrorMessage"] = True
    return json.dumps(row, separators=(",", ":"))


def _bridge(seat: str) -> str:
    """A ``bridge-session`` row (remote control on): the seat of the process writing it."""
    return json.dumps(
        {"type": "bridge-session", "sessionId": "s", "ownerAccountUuid": seat},
        separators=(",", ":"),
    )


def _owner(seat: str) -> str:
    """A ``session-owner`` row: the (sticky) seat the session started on."""
    return json.dumps(
        {"type": "session-owner", "accountUuid": seat, "sessionId": "s"},
        separators=(",", ":"),
    )


def _local(kind: str, epoch: float, *, filler: str = "") -> str:
    """A compact LOCAL transcript entry (no API call): ``mode`` / ``bridge-session`` / …."""
    payload: dict[str, object] = {"type": kind, "timestamp": _iso(epoch)}
    if filler:
        payload["note"] = filler
    return json.dumps(payload, separators=(",", ":"))


def _write(path: Path, lines: list[str], *, mtime: float) -> Path:
    """Write *lines* as JSONL (trailing newline) and stamp *mtime* on the file."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


class _FakeAdapter:
    """Minimal adapter exposing only ``transcript_path`` (the probed capability)."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        return self._path


def _session() -> Session:
    return Session(session_id="s", cwd="/repo", aim="x")


# --------------------------------------------------------------------------- anchor reach
def test_transcript_anchor_none_without_path_or_capability(tmp_path: Path) -> None:
    session = _session()
    now = 1_000_000.0
    assert cachettl.transcript_anchor(_FakeAdapter(None), session, now, 3600) is None
    assert cachettl.transcript_anchor(object(), session, now, 3600) is None
    missing = _FakeAdapter(tmp_path / "missing.jsonl")
    assert cachettl.transcript_anchor(missing, session, now, 3600) is None


def test_countdown_for_no_transcript_is_empty() -> None:
    assert cachettl.countdown_for(_FakeAdapter(None), _session()) == ("", "")


# --------------------------------------------------------------------------- warm / anchor
def test_countdown_for_warm_from_newest_assistant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Newest line is a fresh main-chain assistant entry → ♨ green off THAT timestamp."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    lines = [_assistant(now - 3000), _assistant(now - 300)]  # newest turn 5 min ago
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)
    text, level = cachettl.countdown_for(_FakeAdapter(path), _session(), now=now)
    assert level == "green"
    assert text == "♨ 55:00"  # 3600 - 300 = 3300 s remaining


# --------------------------------------------------------------------------- THE regression
def test_stale_assistant_with_fresh_local_entries_reads_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume-appended local entries (fresh mtime) must NOT fake a warm clock.

    A 3-day-old assistant turn followed by fresh ``mode`` / ``bridge-session`` / … local
    lines (and the file utime'd to now) has a warm-looking mtime but a stone-cold cache —
    the anchor is the OLD assistant entry, so the cell reads ``❄ cold``.
    """
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    old = now - 3 * 86400
    lines = [
        _assistant(old),
        _local("mode", now - 5),
        _local("bridge-session", now - 4),
        _local("permission-mode", now - 3),
        _local("file-history-snapshot", now - 2),
    ]
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)  # fresh mtime, stale cache
    assert cachettl.countdown_for(_FakeAdapter(path), _session(), now=now) == ("❄ cold", "red")


def test_sidechain_assistant_is_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh ``isSidechain:true`` assistant refreshes a DIFFERENT prefix → still cold."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    lines = [_assistant(now - 3 * 86400), _assistant(now - 5, sidechain=True)]
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)
    assert cachettl.countdown_for(_FakeAdapter(path), _session(), now=now) == ("❄ cold", "red")


def test_no_assistant_lines_fresh_mtime_reads_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly opened session with only local entries (no API turn yet) is cold."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    lines = [_local("mode", now - 5), _local("bridge-session", now - 4)]
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)
    assert cachettl.countdown_for(_FakeAdapter(path), _session(), now=now) == ("❄ cold", "red")


# --------------------------------------------------------------------------- fast path
def test_fast_path_cold_never_opens_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mtime already older than the TTL → cold via pure stat math, transcript never read."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    # A warm-looking assistant line, but the FILE mtime is 4000 s old (> 3600 TTL).
    path = _write(tmp_path / "t.jsonl", [_assistant(now - 100)], mtime=now - 4000)

    def _no_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("fast path must not open the transcript")

    monkeypatch.setattr(Path, "open", _no_open)
    assert cachettl.countdown_for(_FakeAdapter(path), _session(), now=now) == ("❄ cold", "red")
    assert path not in cachettl._ANCHOR_CACHE  # fast path never populates the read-cache


# --------------------------------------------------------------------------- chunk boundary
def test_chunk_boundary_qualifying_line_far_before_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only assistant line sits well over a chunk before EOF (and is itself longer than
    a chunk) → the backwards reader still reassembles and finds it."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    monkeypatch.setattr(cachettl, "_CHUNK_BYTES", 32)
    monkeypatch.setattr(cachettl, "_MAX_CHUNK_BYTES", 64)
    now = 1_000_000.0
    filler = "x" * 500  # each pad line dwarfs the 32-byte chunk
    pad = [_local("mode", now - 1, filler=filler) for _ in range(3)]
    lines = [_assistant(now - 300), *pad]  # target buried ~1.5 KiB before EOF
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)
    text, level = cachettl.countdown_for(_FakeAdapter(path), _session(), now=now)
    assert level == "green"
    assert text == "♨ 55:00"


# --------------------------------------------------------------------------- anchor cache
def test_anchor_cache_reuses_until_mtime_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same mtime → transcript read once; a moved mtime forces a re-read."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    path = _write(tmp_path / "t.jsonl", [_assistant(now - 300)], mtime=now)
    calls = {"n": 0}
    real_scan = cachettl._scan_anchor

    def _counting(scan_path: Path) -> tuple[float, str | None] | None:
        calls["n"] += 1
        return cast("tuple[float, str | None] | None", real_scan(scan_path))

    monkeypatch.setattr(cachettl, "_scan_anchor", _counting)
    adapter = _FakeAdapter(path)
    session = _session()
    cachettl.countdown_for(adapter, session, now=now)
    cachettl.countdown_for(adapter, session, now=now)
    assert calls["n"] == 1  # unchanged transcript → read at most once
    os.utime(path, (now + 1, now + 1))  # bump mtime
    cachettl.countdown_for(adapter, session, now=now)
    assert calls["n"] == 2  # mtime moved → re-read


# --------------------------------------------------------------------------- env × anchor
def test_env_ttl_override_applies_to_countdown_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CC_CACHE_TTL_S`` drives the anchor-based countdown end to end."""
    monkeypatch.setenv("CC_CACHE_TTL_S", "1800")
    now = 1_000_000.0
    path = _write(tmp_path / "t.jsonl", [_assistant(now - 600)], mtime=now)
    # ttl 1800, 600 s since the last turn → 1200 s remaining → green.
    assert cachettl.countdown_for(_FakeAdapter(path), _session(), now=now) == ("♨ 20:00", "green")


# --------------------------------------------------------------------------- running rows
def test_countdown_for_running_row_is_play_icon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy row reads ``▶`` (level ``running``) instead of a countdown — even warm."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    path = _write(tmp_path / "t.jsonl", [_assistant(now - 300)], mtime=now)
    adapter = _FakeAdapter(path)
    # Same session, same transcript: warm without the flag, ▶ with it.
    assert cachettl.countdown_for(adapter, _session(), now=now) == ("♨ 55:00", "green")
    assert cachettl.countdown_for(adapter, _session(), now=now, running=True) == ("▶", "running")


def test_countdown_for_running_row_matches_working_status_icon() -> None:
    """The busy glyph stays the WORKING status icon (cachettl deliberately avoids the import)."""
    from command_center.models import STATUS_ICON, Status

    assert cachettl._RUNNING_GLYPH == STATUS_ICON[Status.WORKING]


def test_countdown_for_running_row_reads_no_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The short-circuit happens before any stat/read — busy rows cost no transcript I/O."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    path = _write(tmp_path / "t.jsonl", [_assistant(now - 300)], mtime=now)

    def _no_open(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a running row must not open the transcript")

    def _no_stat(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a running row must not stat the transcript")

    monkeypatch.setattr(Path, "open", _no_open)
    monkeypatch.setattr(Path, "stat", _no_stat)
    assert cachettl.countdown_for(_FakeAdapter(path), _session(), now=now, running=True) == (
        "▶",
        "running",
    )
    assert path not in cachettl._ANCHOR_CACHE


# --------------------------------------------------------------------------- fixed width
def test_cell_padding_reserves_a_constant_width() -> None:
    """Every possible cell — countdown, cold, busy, empty — pads to the same width."""
    for text in ("♨ 59:26", "♨ 9:59", "❄ cold", "▶", ""):
        assert len(text) + len(cachettl.cell_padding(text)) == cachettl.CELL_WIDTH, text


def test_cell_padding_never_truncates_over_wide_content() -> None:
    """A longer cell (custom CC_CACHE_TTL_S past 99 min) keeps at least the separator space."""
    wide = "♨ 120:00"
    assert len(wide) >= cachettl.CELL_WIDTH
    assert cachettl.cell_padding(wide) == " "


def test_cell_width_measured_by_len_matches_terminal_cells() -> None:
    """``len()`` is a valid width measure here: every glyph this cell holds is one cell wide.

    Pins the assumption :data:`cachettl.CELL_WIDTH` rests on — if a future glyph is wide
    (an emoji-presentation variant, say), padding by ``len()`` would silently misalign the
    column and this fails first.
    """
    from rich.cells import cell_len

    for text in ("♨ 59:26", "❄ cold", cachettl._RUNNING_GLYPH):
        assert cell_len(text) == len(text), text


# --------------------------------------------------------------------------- tp#2243 predicates
_FIXTURE = Path(__file__).parent / "fixtures" / "warm_anchor_cases.jsonl"


def test_fixture_cases() -> None:
    """Every shared case (byte copy of the hooks' ``testdata/warm_anchor_cases.jsonl``)
    gets the same ``is_real_response`` / ``is_cache_anchor`` verdict as in the other three
    implementations (hooks ``cc_warm.py``, the statusline, decide)."""
    cases = [json.loads(line) for line in _FIXTURE.read_text(encoding="utf-8").splitlines()]
    cases = [case for case in cases if case]
    assert len(cases) >= 10
    for case in cases:
        row = case["row"]
        assert cachettl.is_real_response(row) is case["real_response"], case["case"]
        assert cachettl.is_cache_anchor(row) is case["cache_anchor"], case["case"]


def test_refusal_never_anchors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The usage-limit refusal (``isApiErrorMessage``, ``<synthetic>``, zero usage) written
    a minute ago after a 30-hour-old real request: the cache is cold, not ``♨ 59:00``."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    lines = [
        _assistant(now - 30 * 3600),
        _assistant(now - 60, cache_read=0, model="<synthetic>", error=True),
    ]
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)
    assert cachettl.countdown_for(_FakeAdapter(path), _session(), now=now) == ("❄ cold", "red")


def test_refusal_is_skipped_back_to_the_real_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan keeps reading back past a refusal to the newest real cache request."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    lines = [
        _assistant(now - 600),
        _assistant(now - 60, cache_read=0, model="<synthetic>", error=True),
    ]
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)
    assert cachettl.countdown_for(_FakeAdapter(path), _session(), now=now) == ("♨ 50:00", "green")


def test_zero_usage_response_never_anchors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid model response that read and wrote no cache does not refresh the clock."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    path = _write(tmp_path / "t.jsonl", [_assistant(now - 60, cache_read=0)], mtime=now)
    assert cachettl.countdown_for(_FakeAdapter(path), _session(), now=now) == ("❄ cold", "red")
    assert cachettl.is_real_response(json.loads(_assistant(now, cache_read=0)))


def test_unparsable_assistant_line_never_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated assistant line cannot show its usage, so it never anchors."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    path = _write(tmp_path / "t.jsonl", [_assistant(now - 60)[:-5]], mtime=now)
    assert cachettl.countdown_for(_FakeAdapter(path), _session(), now=now) == ("❄ cold", "red")


# --------------------------------------------------------------------------- tp#2243 seats
_SEAT_A = "00000000-0000-4000-8000-00000000000a"
_SEAT_B = "00000000-0000-4000-8000-00000000000b"


def _seat_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Two configured accounts whose ``.claude.json`` name seat A and seat B."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    monkeypatch.setattr(accounts, "is_multi_account", lambda: True)
    dirs: dict[str, str] = {}
    for label, seat in (("a", _SEAT_A), ("b", _SEAT_B)):
        folder = tmp_path / f"seat-{label}"
        folder.mkdir()
        (folder / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": seat}}), encoding="utf-8"
        )
        dirs[label] = str(folder)
    return dirs


def _seated(config_dir: str) -> Session:
    return Session(session_id="s", cwd="/repo", aim="x", config_dir=config_dir)


def test_same_seat_is_warm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anchor written on seat A, the session runs on seat A → the countdown shows."""
    dirs = _seat_dirs(tmp_path, monkeypatch)
    now = 1_000_000.0
    path = _write(tmp_path / "t.jsonl", [_bridge(_SEAT_A), _assistant(now - 300)], mtime=now)
    cell = cachettl.countdown_for(_FakeAdapter(path), _seated(dirs["a"]), now=now)
    assert cell == ("♨ 55:00", "green")


def test_seat_change_reads_cold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anchor on seat A, then the tab switched to seat B (a newer bridge row for B, no new
    request): the cache belongs to A, so the B session reads cold."""
    dirs = _seat_dirs(tmp_path, monkeypatch)
    now = 1_000_000.0
    lines = [_bridge(_SEAT_A), _assistant(now - 300), _bridge(_SEAT_B)]
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)
    cell = cachettl.countdown_for(_FakeAdapter(path), _seated(dirs["b"]), now=now)
    assert cell == ("❄ cold", "red")


def test_reopen_on_other_seat_without_new_bridge_row_reads_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old bridge A, anchor A, reopened on seat B with remote control off (no new bridge
    row): the newest bridge row is NOT the running seat; ``config_dir`` B decides → cold."""
    dirs = _seat_dirs(tmp_path, monkeypatch)
    now = 1_000_000.0
    lines = [_bridge(_SEAT_A), _assistant(now - 300), _local("mode", now - 5)]
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)
    cell = cachettl.countdown_for(_FakeAdapter(path), _seated(dirs["b"]), now=now)
    assert cell == ("❄ cold", "red")


def test_unknown_seat_reads_cold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: no seat row before the anchor, or an unknown running seat → cold."""
    dirs = _seat_dirs(tmp_path, monkeypatch)
    now = 1_000_000.0
    adapter = _FakeAdapter(_write(tmp_path / "t.jsonl", [_assistant(now - 300)], mtime=now))
    assert cachettl.countdown_for(adapter, _seated(dirs["a"]), now=now) == ("❄ cold", "red")
    cachettl._ANCHOR_CACHE.clear()
    seated = _FakeAdapter(
        _write(tmp_path / "u.jsonl", [_bridge(_SEAT_A), _assistant(now - 300)], mtime=now)
    )
    assert cachettl.countdown_for(seated, _seated(""), now=now) == ("❄ cold", "red")
    missing = str(tmp_path / "no-such-account")
    assert cachettl.countdown_for(seated, _seated(missing), now=now) == ("❄ cold", "red")


def test_seat_row_past_the_scan_bound_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bridge row further back than ``_SEAT_SCAN_BYTES`` before the anchor is not read."""
    dirs = _seat_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(cachettl, "_SEAT_SCAN_BYTES", 1024)
    now = 1_000_000.0
    pad = [_local("mode", now - 900, filler="x" * 600) for _ in range(3)]
    lines = [_bridge(_SEAT_A), *pad, _assistant(now - 300)]
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)
    cell = cachettl.countdown_for(_FakeAdapter(path), _seated(dirs["a"]), now=now)
    assert cell == ("❄ cold", "red")


def test_no_bridge_rows_falls_back_to_session_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remote control never on: the ``session-owner`` row names the anchor's seat."""
    dirs = _seat_dirs(tmp_path, monkeypatch)
    now = 1_000_000.0
    lines = [_owner(_SEAT_A), _assistant(now - 300), _owner(_SEAT_A)]
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)
    assert cachettl.countdown_for(_FakeAdapter(path), _seated(dirs["a"]), now=now) == (
        "♨ 55:00",
        "green",
    )
    assert cachettl.countdown_for(_FakeAdapter(path), _seated(dirs["b"]), now=now) == (
        "❄ cold",
        "red",
    )


def test_bridge_row_beats_a_nearer_session_owner_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``session-owner`` is sticky (the start seat); a bridge row at or before the anchor
    names the seat of the process that made the request, so it wins."""
    dirs = _seat_dirs(tmp_path, monkeypatch)
    now = 1_000_000.0
    lines = [_bridge(_SEAT_B), _owner(_SEAT_A), _assistant(now - 300)]
    path = _write(tmp_path / "t.jsonl", lines, mtime=now)
    cell = cachettl.countdown_for(_FakeAdapter(path), _seated(dirs["b"]), now=now)
    assert cell == ("♨ 55:00", "green")


def test_single_account_skips_the_seat_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One configured account: no seat rows and no ``config_dir`` still read warm."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    monkeypatch.setattr(accounts, "is_multi_account", lambda: False)
    now = 1_000_000.0
    path = _write(tmp_path / "t.jsonl", [_assistant(now - 300)], mtime=now)
    assert cachettl.countdown_for(_FakeAdapter(path), _session(), now=now) == ("♨ 55:00", "green")


def test_same_mtime_changed_size_rescans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An append that keeps the mtime (same tick) but grows the file is re-scanned."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    now = 1_000_000.0
    path = _write(tmp_path / "t.jsonl", [_assistant(now - 3000)], mtime=now)
    adapter = _FakeAdapter(path)
    assert cachettl.countdown_for(adapter, _session(), now=now) == ("♨ 10:00", "orange")
    stamp = path.stat().st_mtime_ns
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_assistant(now - 300) + "\n")
    os.utime(path, ns=(stamp, stamp))
    assert cachettl.countdown_for(adapter, _session(), now=now) == ("♨ 55:00", "green")
