"""The one-pass transcript collector + the peek TranscriptCache (PLAN_panel-server S3a).

The contract under test: whatever order and granularity the bytes arrive in, the
collector's output equals today's two full walks —
``ClaudeAdapter.all_user_prompts_in_file`` (prompts tab) and
``sessionmd.session_segments(events_in_file(path))`` (session tab).
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
from pathlib import Path
from typing import BinaryIO

import pytest

from command_center import peek, sessionmd, transcript_cache
from command_center.adapters import ClaudeAdapter
from command_center.adapters.claude import events_in_file
from command_center.store import Store
from command_center.transcript_cache import (
    TranscriptCache,
    TranscriptCollector,
    collect_file,
    read_view,
)


# ---------------------------------------------------------------------------
# fixture transcripts
# ---------------------------------------------------------------------------
def _user(content: object, **extra: object) -> dict:
    record: dict = {"type": "user", "message": {"role": "user", "content": content}}
    record.update(extra)
    return record


def _assistant(content: object, **extra: object) -> dict:
    record: dict = {"type": "assistant", "message": {"role": "assistant", "content": content}}
    record.update(extra)
    return record


def _tool_use(tool_id: str, name: str, tool_input: dict) -> dict:
    return _assistant([{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}])


def _tool_result(tool_id: str, text: str) -> dict:
    return _user([{"type": "tool_result", "tool_use_id": tool_id, "content": text}])


def _queued(prompt: object, mode: str = "prompt", origin: object = None) -> dict:
    return {
        "type": "attachment",
        "attachment": {
            "type": "queued_command",
            "prompt": prompt,
            "commandMode": mode,
            "origin": origin,
        },
    }


RICH: list[dict] = [
    _user("fix the failing test"),
    _assistant(
        [
            {"type": "thinking", "thinking": "secret reasoning"},
            {"type": "text", "text": "Looking at the test."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -x"}},
        ]
    ),
    _user(
        [
            {
                "type": "tool_result",
                "tool_use_id": "t1",
                "content": [{"type": "text", "text": "1 failed"}],
            }
        ]
    ),
    _assistant([{"type": "text", "text": "subagent chatter"}], isSidechain=True),
    _user("sidechain prompt", isSidechain=True),
    _user("meta noise", isMeta=True),
    _user("<task-notification><task-id>a</task-id></task-notification>"),
    _queued("queued while busy"),
    _queued("<task-notification>x</task-notification>", mode="task-notification"),
    _queued("from a peer", origin={"kind": "peer"}),
    _user([{"type": "text", "text": "see [Image #1]"}, {"type": "image", "source": {}}]),
    _user("<command-name>/aim</command-name><command-args>x</command-args>real ask ünïcödé ✓"),
    _tool_use("t2", "Edit", {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}),
    _tool_use("t3", "Read", {"file_path": "/tmp/never-answered.py"}),
    _tool_result("t2", "edited\nline two\nline three\nline four"),
    _assistant("Fixed. ```python\nprint(1)\n"),
    _user("thanks, one more"),
]


def _lines(records: list[dict]) -> list[str]:
    return [json.dumps(record, ensure_ascii=False) for record in records]


FIXTURES: dict[str, bytes] = {
    "rich": ("\n".join(_lines(RICH)) + "\n").encode(),
    "no-trailing-newline": "\n".join(_lines(RICH)).encode(),
    "crlf-blank-malformed": (
        "\r\n".join(
            _lines(RICH[:5]) + ["", "   ", "{not json", "null", "[1, 2]"] + _lines(RICH[5:])
        )
        + "\r\n"
    ).encode(),
    "only-prompts": ("\n".join(_lines([_user(f"ask {n}") for n in range(20)])) + "\n").encode(),
    "empty": b"",
    "preamble-only": (
        "\n".join(_lines([_assistant("resumed tail"), _tool_result("zz", "x")])) + "\n"
    ).encode(),
}


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(data)


def _today(path: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """What the two pre-S3a walks return for *path* (the parity oracle)."""
    prompts = ClaudeAdapter(claude_home=path.parent).all_user_prompts_in_file(path)
    return prompts, sessionmd.session_segments(events_in_file(path))


def _as_tuple(view: transcript_cache.TranscriptView) -> tuple[list[str], list[tuple[str, str]]]:
    return view.prompts, view.segments


# ---------------------------------------------------------------------------
# one-pass parity
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_one_pass_equals_the_two_walks(name: str, tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", FIXTURES[name])
    prompts, events = collect_file(path)
    assert prompts == ClaudeAdapter(claude_home=tmp_path).all_user_prompts_in_file(path)
    assert events == events_in_file(path)
    assert _as_tuple(read_view(path)) == _today(path)
    view, state = TranscriptCache().get(path)
    assert state == "miss"
    assert _as_tuple(view) == _today(path)


def test_rich_fixture_exercises_the_filters(tmp_path: Path) -> None:
    """Guard the fixture itself: it must hit pairing, sidechain, meta and queued paths."""
    path = _write(tmp_path / "t.jsonl", FIXTURES["rich"])
    prompts, events = collect_file(path)
    assert prompts == [
        "fix the failing test",
        "queued while busy",
        "see [Image #1]",
        "real ask ünïcödé ✓",
        "thanks, one more",
    ]
    tools = {e.tool_name: e.tool_result for e in events if e.kind == "tool"}
    assert tools == {
        "Bash": "1 failed",
        "Edit": "edited\nline two\nline three\nline four",
        "Read": None,
    }


def test_missing_file_is_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nope.jsonl"
    assert collect_file(missing) == ([], [])
    assert read_view(missing) == transcript_cache.empty_view()
    cache = TranscriptCache()
    assert cache.get(missing) == (transcript_cache.empty_view(), "miss")
    assert len(cache) == 0


@pytest.mark.parametrize("name", ["rich", "no-trailing-newline", "crlf-blank-malformed"])
def test_every_two_way_split_equals_the_full_parse(name: str, tmp_path: Path) -> None:
    """Split the bytes at EVERY offset: a view mid-way (provisional tail) must not leak."""
    data = FIXTURES[name]
    path = _write(tmp_path / "t.jsonl", data)
    expected = _today(path)
    for split in range(len(data) + 1):
        collector = TranscriptCollector()
        collector.feed(data[:split])
        collector.view()  # applies + rolls back the unterminated tail
        collector.feed(data[split:])
        assert _as_tuple(collector.view()) == expected, split
        assert collector.consumed == len(data)


def test_multibyte_char_split_across_feeds(tmp_path: Path) -> None:
    data = FIXTURES["rich"]
    cut = data.index("ü".encode()) + 1  # inside the two-byte sequence
    path = _write(tmp_path / "t.jsonl", data)
    collector = TranscriptCollector()
    collector.feed(data[:cut])
    collector.feed(data[cut:])
    assert _as_tuple(collector.view()) == _today(path)


def test_invalid_utf8_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    good = "\n".join(_lines([_user("before"), _user("after")])).encode()
    bad = b'{"type": "user", "message": {"role": "user", "content": "\xff\xfe"}}\n'
    first, second = good.split(b"\n")
    path = _write(tmp_path / "t.jsonl", first + b"\n" + bad + second + b"\n")
    assert read_view(path).prompts == ["before", "after"]


# ---------------------------------------------------------------------------
# incremental cache states
# ---------------------------------------------------------------------------
def test_record_split_across_two_appends(tmp_path: Path) -> None:
    lines = [line.encode() + b"\n" for line in _lines(RICH)]
    head = b"".join(lines[:4])
    split_line = lines[4]
    path = _write(tmp_path / "t.jsonl", head + split_line[: len(split_line) // 2])
    cache = TranscriptCache()
    _view, state = cache.get(path)
    assert state == "miss"
    _append(path, split_line[len(split_line) // 2 :] + b"".join(lines[5:]))
    view, state = cache.get(path)
    assert state == "append"
    assert _as_tuple(view) == _today(path)


def test_tool_result_arrives_in_a_later_append(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        (
            "\n".join(_lines([_user("go"), _tool_use("x1", "Bash", {"command": "make"})])) + "\n"
        ).encode(),
    )
    cache = TranscriptCache()
    first, state = cache.get(path)
    assert state == "miss"
    assert "⎿" not in "".join(t for t, _ in first.segments)
    _append(path, (json.dumps(_tool_result("x1", "build ok")) + "\n").encode())
    view, state = cache.get(path)
    assert state == "append"
    assert _as_tuple(view) == _today(path)
    assert "⎿ build ok" in "".join(t for t, _ in view.segments)
    # the earlier view object is a snapshot — not mutated by the later pairing
    assert "⎿" not in "".join(t for t, _ in first.segments)


def test_hit_returns_the_same_object_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "t.jsonl", FIXTURES["rich"])
    cache = TranscriptCache()
    first, state = cache.get(path)
    assert state == "miss"

    def _no_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a hit must not open the transcript")

    monkeypatch.setattr(transcript_cache, "open", _no_read, raising=False)
    second, state = cache.get(path)
    assert state == "hit"
    assert second is first


def test_inode_replacement_is_a_miss(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", FIXTURES["rich"])
    cache = TranscriptCache()
    cache.get(path)
    # Same path, new inode, LARGER content (would look like an append by size alone).
    replacement = _write(tmp_path / "new.jsonl", FIXTURES["rich"] + FIXTURES["only-prompts"])
    os.replace(replacement, path)
    view, state = cache.get(path)
    assert state == "miss"
    assert _as_tuple(view) == _today(path)


def test_truncation_is_a_miss(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", FIXTURES["rich"])
    cache = TranscriptCache()
    cache.get(path)
    with path.open("r+b") as handle:  # same inode, shrunk in place
        handle.truncate(len(FIXTURES["rich"]) // 3)
    view, state = cache.get(path)
    assert state == "miss"
    assert _as_tuple(view) == _today(path)


def test_same_size_new_mtime_is_a_miss(tmp_path: Path) -> None:
    data = FIXTURES["only-prompts"]
    path = _write(tmp_path / "t.jsonl", data)
    cache = TranscriptCache()
    cache.get(path)
    st = path.stat()
    with path.open("r+b") as handle:  # rewrite in place, same length
        handle.write(data.replace(b"ask 1", b"ASK 1"))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    view, state = cache.get(path)
    assert state == "miss"
    assert "ASK 1" in view.prompts


def test_read_failure_drops_the_entry_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "t.jsonl", FIXTURES["rich"])
    cache = TranscriptCache()
    cache.get(path)
    _append(path, (json.dumps(_user("appended ask")) + "\n").encode())

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(transcript_cache, "open", _fail, raising=False)
    view, state = cache.get(path)
    assert state == "miss"
    assert view == transcript_cache.empty_view()  # the fallback read failed too
    assert len(cache) == 0
    monkeypatch.undo()
    view, state = cache.get(path)
    assert state == "miss"
    assert _as_tuple(view) == _today(path)
    assert view.prompts[-1] == "appended ask"


def test_failure_mid_append_rebuilds_from_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read error AFTER the collector was partly fed must not leave a corrupt entry."""
    path = _write(tmp_path / "t.jsonl", FIXTURES["rich"])
    cache = TranscriptCache()
    cache.get(path)
    _append(path, FIXTURES["only-prompts"])
    real_feed = transcript_cache._feed_from  # pylint: disable=protected-access
    calls = {"n": 0}

    def _flaky(handle: BinaryIO, collector: TranscriptCollector, nbytes: int) -> int:
        calls["n"] += 1
        if calls["n"] == 1:  # the append: feed a little, then fail
            collector.feed(handle.read(10))
            raise OSError("disk went away")
        return real_feed(handle, collector, nbytes)

    monkeypatch.setattr(transcript_cache, "_feed_from", _flaky)
    view, state = cache.get(path)
    assert state == "miss"
    assert _as_tuple(view) == _today(path)
    assert len(cache) == 1  # the rebuilt entry replaced the dropped one


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------
def test_lru_eviction_by_entry_count(tmp_path: Path) -> None:
    paths = [_write(tmp_path / f"{n}.jsonl", FIXTURES["only-prompts"]) for n in range(3)]
    cache = TranscriptCache(max_entries=2)
    cache.get(paths[0])
    cache.get(paths[1])
    assert cache.get(paths[0])[1] == "hit"  # 0 is now most recent → 1 is the LRU
    cache.get(paths[2])
    assert len(cache) == 2
    assert cache.get(paths[0])[1] == "hit"
    assert cache.get(paths[1])[1] == "miss"  # evicted


def test_lru_eviction_by_byte_cap(tmp_path: Path) -> None:
    size = len(FIXTURES["only-prompts"])
    paths = [_write(tmp_path / f"{n}.jsonl", FIXTURES["only-prompts"]) for n in range(3)]
    cache = TranscriptCache(max_entries=8, max_bytes=2 * size + 1)
    cache.get(paths[0])
    cache.get(paths[1])
    assert cache.bytes_used == 2 * size
    cache.get(paths[2])
    assert len(cache) == 2
    assert cache.bytes_used == 2 * size
    assert cache.get(paths[0])[1] == "miss"  # the least recently used went first


def test_oversize_file_is_served_but_not_retained(tmp_path: Path) -> None:
    path = _write(tmp_path / "big.jsonl", FIXTURES["rich"])
    cache = TranscriptCache(max_bytes=len(FIXTURES["rich"]) - 1)
    view, state = cache.get(path)
    assert state == "miss"
    assert _as_tuple(view) == _today(path)
    assert len(cache) == 0
    assert cache.bytes_used == 0


def test_append_past_the_byte_cap_drops_the_entry(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", FIXTURES["only-prompts"])
    cache = TranscriptCache(max_bytes=len(FIXTURES["only-prompts"]) + 10)
    cache.get(path)
    assert len(cache) == 1
    _append(path, FIXTURES["only-prompts"])
    view, state = cache.get(path)
    assert state == "append"
    assert _as_tuple(view) == _today(path)
    assert len(cache) == 0


def test_clear_and_prewarm(tmp_path: Path) -> None:
    paths = [_write(tmp_path / f"{n}.jsonl", FIXTURES["rich"]) for n in range(2)]
    cache = TranscriptCache()
    cache.prewarm([*paths, tmp_path / "missing.jsonl"])
    assert len(cache) == 2
    assert cache.get(paths[1])[1] == "hit"
    cache.clear()
    assert len(cache) == 0
    assert cache.bytes_used == 0


def test_symlinked_path_shares_the_canonical_entry(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", FIXTURES["rich"])
    link = tmp_path / "link.jsonl"
    link.symlink_to(path)
    cache = TranscriptCache()
    cache.get(path)
    assert cache.get(link)[1] == "hit"
    assert len(cache) == 1


# ---------------------------------------------------------------------------
# thread safety
# ---------------------------------------------------------------------------
def test_concurrent_gets_on_appending_files(tmp_path: Path) -> None:
    lines = [line.encode() + b"\n" for line in _lines(RICH * 5)]
    paths = [_write(tmp_path / f"{n}.jsonl", b"") for n in range(2)]
    cache = TranscriptCache(max_entries=2)
    errors: list[BaseException] = []
    done = threading.Event()

    def writer(path: Path) -> None:
        try:
            for line in lines:  # every record lands in two halves
                _append(path, line[: len(line) // 2])
                _append(path, line[len(line) // 2 :])
        except BaseException as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            errors.append(exc)

    def reader() -> None:
        try:
            while not done.is_set():
                for path in paths:
                    cache.get(path)
        except BaseException as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            errors.append(exc)

    writers = [threading.Thread(target=writer, args=(path,)) for path in paths]
    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers + writers:
        thread.start()
    for thread in writers:
        thread.join()
    done.set()
    for thread in readers:
        thread.join()
    assert not errors
    for path in paths:
        view, _state = cache.get(path)
        assert _as_tuple(view) == _today(path)


# ---------------------------------------------------------------------------
# peek wiring
# ---------------------------------------------------------------------------
def _transcript(home: Path, cwd: str, session_id: str, records: list[dict]) -> Path:
    path = home / "projects" / cwd.replace("/", "-") / f"{session_id}.jsonl"
    return _write(path, ("\n".join(_lines(records)) + "\n").encode())


def _no_state(data: peek.PeekData) -> peek.PeekData:
    return dataclasses.replace(data, cache_state="")


def test_resolve_peek_cached_equals_cold_and_reports_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")
    store.ensure("sid", cwd="/Users/x/repo")
    store.update_fields("sid", iterm_session_id="w0t1p0:UUID-A", last_response_at=10)
    store.set_aim("sid", "first aim")
    path = _transcript(tmp_path, "/Users/x/repo", "sid", RICH)
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "UUID-A")
    cache = TranscriptCache()

    cold = peek.resolve_peek(adapter=adapter, store=store)
    assert cold.cache_state == "miss"
    assert cold.prompts == adapter.all_user_prompts("/Users/x/repo", "sid")
    assert cold.session_segments == sessionmd.segments_for_path(path)

    states = []
    for step in range(3):
        if step == 2:
            _append(path, (json.dumps(_user("new ask")) + "\n").encode())
            store.set_aim("sid", "second aim")  # header data is never cached
        warm = peek.resolve_peek(adapter=adapter, store=store, cache=cache)
        cold = peek.resolve_peek(adapter=adapter, store=store)
        assert _no_state(warm) == _no_state(cold)
        states.append(warm.cache_state)
    assert states == ["miss", "hit", "append"]
    assert warm.prompts[-1] == "new ask"
    assert [rev.aim for rev in warm.aim_revisions] == ["first aim", "second aim"]
    store.close()


def test_resolve_peek_fallback_uses_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")
    _transcript(tmp_path, "/Users/x/repo", "loose", RICH)
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "NO-MATCH")
    monkeypatch.setattr(peek, "frontmost_iterm_cwd", lambda: "/Users/x/repo")
    cache = TranscriptCache()
    cold = peek.resolve_peek(adapter=adapter, store=store)
    first = peek.resolve_peek(adapter=adapter, store=store, cache=cache)
    second = peek.resolve_peek(adapter=adapter, store=store, cache=cache)
    assert (cold.cache_state, first.cache_state, second.cache_state) == ("miss", "miss", "hit")
    assert _no_state(cold) == _no_state(first) == _no_state(second)
    assert cold.session_id == "loose" and cold.prompts
    store.close()


def test_resolve_peek_missing_transcript_reads_nothing(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    store = Store(tmp_path / "state.db")
    store.ensure("sid", cwd="/Users/x/repo")
    data = peek.resolve_peek(
        adapter=adapter, store=store, session_id="sid", cache=TranscriptCache()
    )
    assert data.resolved
    assert data.cache_state == ""
    assert not data.prompts
    assert data.session_segments == sessionmd.session_segments([])
    store.close()
