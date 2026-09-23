#!/usr/bin/env python3
"""Benchmark the peek panel's transcript read: two walks vs one pass vs the cache.

Times, on one transcript (PLAN_panel-server.md S3a / D5a):

* **two-walk** — today's pre-S3a peek read: ``all_user_prompts_in_file`` +
  ``sessionmd.segments_for_path`` (its mtime cache cleared, i.e. uncached);
* **one-pass (cold)** — ``transcript_cache.read_view``: the cold ``ccc peek`` path;
* **miss** — ``TranscriptCache.get`` on an empty cache (full read, entry retained);
* **hit** — ``get`` again on the unchanged file;
* **append** — ``get`` after appending a few realistic records (prompt, assistant
  text + tool call, tool result) to a COPY of the transcript in a temp dir. The real
  transcript is only ever read.

It also checks that the one-pass output equals the two walks (parity) and prints the
process's peak RSS.

Usage:
  tools/bench_peek_cache.py [-p PATH] [-n N]

Options:
  -p, --path PATH    Transcript to measure (default: the largest ``*.jsonl`` under
                     ``~/.claude/projects`` and ``~/.claude-work/projects``).
  -n, --repeat N     Timed repetitions per measurement; min and median are reported
                     (default: 3).
  -h, --help         Show this help and exit.

Examples:
  uv run tools/bench_peek_cache.py
  uv run tools/bench_peek_cache.py -n 5 -p ~/.claude/projects/<dir>/<id>.jsonl
"""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# pylint: disable=wrong-import-position
from command_center import sessionmd, transcript_cache  # noqa: E402
from command_center.adapters import ClaudeAdapter  # noqa: E402

_DEFAULT_ROOTS = (Path.home() / ".claude" / "projects", Path.home() / ".claude-work" / "projects")


def largest_transcript(roots: tuple[Path, ...] = _DEFAULT_ROOTS) -> Path | None:
    """The biggest ``*.jsonl`` below *roots* (``None`` when there is none)."""
    best_size, best_path = -1, None
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > best_size:
                best_size, best_path = size, path
    return best_path


def _time(fn: Callable[[], object], repeat: int) -> list[float]:
    """Wall-clock milliseconds of *repeat* calls of *fn*."""
    out = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        out.append((time.perf_counter() - start) * 1000)
    return out


def _append_records(n: int) -> bytes:
    """A few realistic transcript records: one prompt → tool call → result → reply."""
    tool_id = f"toolu_bench_{n}"
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": f"bench prompt {n}: run the tests"},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Running the test suite now."},
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Bash",
                        "input": {"command": "uv run pytest -q", "description": "Run tests"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": "12 passed in 0.4s"}
                ],
            },
        },
        {"type": "assistant", "message": {"role": "assistant", "content": "All tests pass."}},
    ]
    return "".join(json.dumps(record) + "\n" for record in records).encode()


def _row(label: str, samples: list[float]) -> str:
    return f"| {label:<22} | {min(samples):>9.1f} | {statistics.median(samples):>10.1f} |"


def run(path: Path, repeat: int) -> int:
    """Measure *path*; prints a markdown table. Returns the exit code."""
    adapter = ClaudeAdapter()
    size = path.stat().st_size
    print(f"transcript: {path} ({size / 1e6:.1f} MB), repeat={repeat}")

    def two_walk() -> tuple[list[str], list[tuple[str, str]]]:
        sessionmd._RENDER_CACHE.clear()  # pylint: disable=protected-access
        return adapter.all_user_prompts_in_file(path), sessionmd.segments_for_path(path)

    reference = two_walk()
    one_pass = transcript_cache.read_view(path)
    parity = (one_pass.prompts, one_pass.segments) == reference
    print(f"parity (one-pass == two walks): {'ok' if parity else 'MISMATCH'}")

    rows = [("two-walk (today)", _time(two_walk, repeat))]
    rows.append(("one-pass cold", _time(lambda: transcript_cache.read_view(path), repeat)))
    unbounded = max(size * 4, transcript_cache.DEFAULT_MAX_BYTES)
    rows.append(
        (
            "cache miss",
            _time(lambda: transcript_cache.TranscriptCache(max_bytes=unbounded).get(path), repeat),
        )
    )
    cache = transcript_cache.TranscriptCache(max_bytes=unbounded)
    cache.get(path)
    rows.append(("cache hit", _time(lambda: cache.get(path), repeat)))

    with tempfile.TemporaryDirectory(prefix="bench-peek-cache-") as tmp:
        copy = Path(tmp) / path.name
        shutil.copyfile(path, copy)
        warm = transcript_cache.TranscriptCache(max_bytes=unbounded * 2)
        warm.get(copy)
        samples: list[float] = []
        states: set[str] = set()
        for n in range(repeat):
            with copy.open("ab") as handle:
                handle.write(_append_records(n))
            start = time.perf_counter()
            _view, state = warm.get(copy)
            samples.append((time.perf_counter() - start) * 1000)
            states.add(state)
        rows.append((f"cache append ({'/'.join(sorted(states))})", samples))

    print()
    print(f"| {'measurement':<22} | {'min ms':>9} | {'median ms':>10} |")
    print(f"| {'-' * 22} | {'-' * 8}: | {'-' * 9}: |")
    for label, measured in rows:
        print(_row(label, measured))
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = peak / 1e6 if sys.platform == "darwin" else peak / 1e3  # bytes vs KiB
    print(f"\npeak RSS: {peak_mb:.0f} MB")
    return 0 if parity else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Benchmark peek transcript reads: two walks vs one pass vs TranscriptCache.",
        epilog="Example: uv run tools/bench_peek_cache.py -n 5",
    )
    parser.add_argument("-p", "--path", type=Path, help="transcript (default: largest local)")
    parser.add_argument("-n", "--repeat", type=int, default=3, help="repetitions (default: 3)")
    args = parser.parse_args(argv)
    path = args.path or largest_transcript()
    if path is None or not path.is_file():
        print("no transcript found (pass -p/--path)", file=sys.stderr)
        return 2
    if args.repeat < 1:
        parser.error("-n/--repeat must be >= 1")
    return run(path.expanduser(), args.repeat)


if __name__ == "__main__":
    sys.exit(main())
