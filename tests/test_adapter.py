"""Unit tests for the Claude adapter (uses a fake CLAUDE_HOME fixture)."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from command_center.adapters import ClaudeAdapter
from command_center.models import CODEX_WORKFLOW_NAME


def _write_session(home: Path, pid: int, session_id: str, cwd: str, **extra: object) -> None:
    payload = {"pid": pid, "sessionId": session_id, "cwd": cwd, "status": "idle"}
    payload.update(extra)
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "sessions" / f"{pid}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_discover_and_liveness(tmp_path: Path) -> None:
    _write_session(tmp_path, os.getpid(), "alive-1", "/Users/x/repo", name="n", kind="interactive")
    _write_session(tmp_path, 999_999, "dead-1", "/Users/x/other")
    adapter = ClaudeAdapter(claude_home=tmp_path)

    by_id = {s.session_id: s for s in adapter.discover()}
    assert by_id["alive-1"].alive is True
    assert by_id["alive-1"].name == "n"
    assert by_id["alive-1"].kind == "interactive"
    assert by_id["dead-1"].alive is False


def test_discover_skips_headless_sdk(tmp_path: Path) -> None:
    # Real user sessions carry entrypoint "cli"; headless `claude -p` (the daemon's
    # own summary/grading calls) register with entrypoint "sdk-cli" — they must be
    # skipped so reconcile never persists them as junk "parked" rows. Distinct pids
    # keep the per-pid registry filenames from colliding.
    _write_session(tmp_path, 111, "real", "/Users/x/repo", entrypoint="cli")
    _write_session(tmp_path, 222, "headless", "/", entrypoint="sdk-cli")
    # Missing entrypoint (older Claude builds) defaults to "cli" and is kept.
    _write_session(tmp_path, 333, "legacy", "/Users/x/old")
    adapter = ClaudeAdapter(claude_home=tmp_path)

    by_id = {s.session_id: s for s in adapter.discover()}
    assert "headless" not in by_id
    assert by_id["real"].entrypoint == "cli"
    assert "legacy" in by_id


def test_transcript_path_encoding(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    cwd = "/home/user/projects/infra/home-assistant-sandbox"
    encoded = "-home-user-projects-infra-home-assistant-sandbox"
    target = tmp_path / "projects" / encoded / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")

    assert adapter.transcript_path(cwd, "sid") == target


def test_transcript_path_glob_fallback(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    target = tmp_path / "projects" / "weird-encoding" / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")

    # cwd does not match the directory name, so it must fall back to a glob.
    assert adapter.transcript_path("/does/not/match", "sid") == target


def test_transcript_path_caches_glob_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    target = tmp_path / "projects" / "weird-encoding" / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")

    # First resolution walks the glob fallback and memoizes the hit.
    assert adapter.transcript_path("/does/not/match", "sid") == target

    # A second resolution must be served from the cache: any glob call now fails loudly.
    def _no_glob(self: Path, pattern: str) -> list[Path]:
        raise AssertionError(f"glob should not run on a cache hit: {pattern}")

    monkeypatch.setattr(Path, "glob", _no_glob)
    assert adapter.transcript_path("/does/not/match", "sid") == target


def test_transcript_path_positive_cache_revalidates(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    encoded = "-repo"
    target = tmp_path / "projects" / encoded / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")

    assert adapter.transcript_path("/repo", "sid") == target

    # Deleting the file must invalidate the positive cache: the stale path is never
    # returned; with nothing left to find, resolution falls back to None.
    target.unlink()
    assert adapter.transcript_path("/repo", "sid") is None


def test_transcript_path_negative_ttl_exact_probe_still_lands(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    # Missing transcript → None, negatively cached.
    assert adapter.transcript_path("/repo", "sid") is None

    # Creating it at the EXACT munged path must be found despite the negative cache:
    # the exact-path probe stays live during the TTL, only the glob is skipped.
    target = tmp_path / "projects" / "-repo" / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")
    assert adapter.transcript_path("/repo", "sid") == target


def test_transcript_path_negative_ttl_delays_glob_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.adapters import claude as claude_mod

    adapter = ClaudeAdapter(claude_home=tmp_path)
    # Missing transcript → None, negatively cached.
    assert adapter.transcript_path("/does/not/match", "sid") is None

    # A glob-only location is NOT discovered while the negative cache is trusted.
    target = tmp_path / "projects" / "weird-encoding" / "sid.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")
    assert adapter.transcript_path("/does/not/match", "sid") is None

    # Expiring the TTL re-enables glob discovery.
    monkeypatch.setattr(claude_mod, "_TRANSCRIPT_NEG_TTL", 0.0)
    assert adapter.transcript_path("/does/not/match", "sid") == target


def test_is_oneshot_headless(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)

    # Headless `claude -p` transcript: first record is the enqueued one-shot prompt.
    (proj / "headless.jsonl").write_text(
        json.dumps({"type": "queue-operation", "operation": "enqueue", "content": "..."})
        + "\n"
        + json.dumps({"type": "assistant"})
        + "\n",
        encoding="utf-8",
    )
    # Interactive transcript: opens with session meta, never a queue-operation.
    (proj / "real.jsonl").write_text(json.dumps({"type": "last-prompt"}) + "\n", encoding="utf-8")
    # Leading blank line must be skipped to reach the first real record.
    (proj / "blanky.jsonl").write_text(
        "\n" + json.dumps({"type": "queue-operation"}) + "\n", encoding="utf-8"
    )

    assert adapter.is_oneshot_headless("/repo", "headless") is True
    assert adapter.is_oneshot_headless("/repo", "blanky") is True
    assert adapter.is_oneshot_headless("/repo", "real") is False
    assert adapter.is_oneshot_headless("/repo", "missing") is False  # no transcript


def _rec(**fields: object) -> str:
    return json.dumps(fields)


def _err(text: str) -> str:
    """A rate-limit-style API-error assistant record carrying *text*."""
    return _rec(
        type="assistant",
        isApiErrorMessage=True,
        message={"role": "assistant", "content": [{"type": "text", "text": text}]},
    )


def test_is_halted(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    user = _rec(type="user", message={"role": "user", "content": "go"})
    ok_assistant = _rec(
        type="assistant",
        message={"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    )

    def write(name: str, *records: str) -> None:
        (proj / f"{name}.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")

    # Last turn is a weekly-limit halt → halted.
    write("weekly", user, ok_assistant, _err("You've hit your weekly limit · resets 2pm (Berlin)"))
    # 5-hour "session limit" halt is the same kind of block → also flagged.
    write("session", user, _err("You've hit your session limit · resets 1:10am (Berlin)"))
    # A user prompt that merely *quotes* the phrase is not an API error → not halted.
    write("quoted", _rec(type="user", message={"role": "user", "content": "show 'hit your limit'"}))
    # Resumed past the limit: a fresh *assistant* turn follows the error → no longer halted.
    write("resumed", _err("You've hit your weekly limit · resets 2pm"), user, ok_assistant)
    # Still waiting: a trailing *user* record after the halt (a queued "continue", a
    # background <task-notification>, a slash command) does NOT clear it — the session is
    # still rate-limited. Keying on the last *assistant* record keeps it halted and stops
    # the status flip-flopping HALTED↔WORKING (red ||↔green ▶) while no work is happening.
    write(
        "waiting_user_after",
        user,
        ok_assistant,
        _err("You've hit your weekly limit · resets 2pm"),
        user,
    )
    task_notif = _rec(
        type="user",
        message={"role": "user", "content": "<task-notification>done</task-notification>"},
    )
    write("waiting_task_notif", _err("You've hit your session limit · resets 1:10am"), task_notif)
    # The halt is on a sub-agent side-chain, not the main thread → ignored.
    sidechain_err = _rec(
        type="assistant",
        isApiErrorMessage=True,
        isSidechain=True,
        message={
            "role": "assistant",
            "content": [{"type": "text", "text": "hit your weekly limit"}],
        },
    )
    write("sidechain", user, ok_assistant, sidechain_err)
    # A non-rate-limit API error (overloaded, etc.) is not a halt.
    write("overloaded", user, _err("API Error: Overloaded"))

    assert adapter.is_halted("/repo", "weekly") is True
    assert adapter.is_halted("/repo", "session") is True
    assert adapter.is_halted("/repo", "quoted") is False
    assert adapter.is_halted("/repo", "resumed") is False
    assert adapter.is_halted("/repo", "waiting_user_after") is True
    assert adapter.is_halted("/repo", "waiting_task_notif") is True
    assert adapter.is_halted("/repo", "sidechain") is False
    assert adapter.is_halted("/repo", "overloaded") is False
    assert adapter.is_halted("/repo", "missing") is False  # no transcript


def test_is_halted_scans_only_tail_of_large_transcript(tmp_path: Path) -> None:
    """The halt is the final line; a multi-MB prefix before it must not hide it."""
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    filler = _rec(
        type="assistant",
        message={"role": "assistant", "content": [{"type": "text", "text": "x" * 200}]},
    )
    halt = _err("You've hit your weekly limit · resets 2pm")
    (proj / "big.jsonl").write_text("\n".join([filler] * 2000 + [halt]) + "\n", encoding="utf-8")
    assert adapter.is_halted("/repo", "big") is True


def test_successful_response_since(tmp_path: Path) -> None:
    """The auto-resume "was this resume productive?" signal (Codex O4/O5/O6).

    Only a MAIN-CHAIN assistant record with `isApiErrorMessage` falsy counts — the
    injected prompt + error turn of a barren launch, non-rate API errors, and
    sidechain chatter must all stay "no progress".
    """
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    user = _rec(type="user", message={"role": "user", "content": "go"})
    ok = _rec(
        type="assistant",
        message={"role": "assistant", "content": [{"type": "text", "text": "did work"}]},
    )
    err = _err("You've hit your session limit · resets 1:10am")
    sidechain_ok = _rec(
        type="assistant",
        isSidechain=True,
        message={"role": "assistant", "content": [{"type": "text", "text": "subagent"}]},
    )

    # Productive resume: a successful assistant record lands past the baseline.
    prefix = user + "\n"
    (proj / "good.jsonl").write_text(prefix + ok + "\n" + err + "\n", encoding="utf-8")
    base = len(prefix.encode())
    assert adapter.successful_response_since("/repo", "good", base) is True
    assert adapter.successful_response_since("/repo", "good", 0) is True

    # Barren resume: past the baseline only the injected prompt + the error turn.
    pre = user + "\n" + ok + "\n"
    (proj / "barren.jsonl").write_text(pre + user + "\n" + err + "\n", encoding="utf-8")
    assert adapter.successful_response_since("/repo", "barren", len(pre.encode())) is False

    # A NON-rate API error (overloaded/auth/…) is not progress; nor is sidechain output.
    (proj / "errs.jsonl").write_text(
        prefix + _err("API Error: Overloaded") + "\n" + sidechain_ok + "\n", encoding="utf-8"
    )
    assert adapter.successful_response_since("/repo", "errs", base) is False

    # Offset past EOF (truncation/rotation) → conservative False; missing transcript too.
    assert adapter.successful_response_since("/repo", "good", 10_000_000) is False
    assert adapter.successful_response_since("/repo", "missing", 0) is False

    # A partial trailing record (mid-write) is tolerated, not crashed on.
    (proj / "partial.jsonl").write_text(prefix + ok[: len(ok) // 2], encoding="utf-8")
    assert adapter.successful_response_since("/repo", "partial", base) is False

    # An offset landing MID-line skips that partial first line, then counts the next.
    mid = len((user + "\n").encode()) - 3
    assert adapter.successful_response_since("/repo", "good", mid) is True


def test_halt_reset_at_ms(tmp_path: Path) -> None:
    """The halting error's own reset phrasing becomes the backoff target."""
    from datetime import datetime, timedelta

    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    user = _rec(type="user", message={"role": "user", "content": "go"})
    (proj / "rel.jsonl").write_text(
        user + "\n" + _err("You've hit your weekly limit · resets in 2h 7m") + "\n",
        encoding="utf-8",
    )
    before = datetime.now()
    got = adapter.halt_reset_at_ms("/repo", "rel")
    lo = int((before + timedelta(hours=2, minutes=6)).timestamp() * 1000)
    hi = int((datetime.now() + timedelta(hours=2, minutes=10)).timestamp() * 1000)
    assert lo <= got <= hi  # 2h7m + the script's 1-min safety margin

    # No reset phrasing → 0; a healthy transcript (no error) → 0; missing → 0.
    (proj / "vague.jsonl").write_text(_err("You've hit your weekly limit") + "\n", encoding="utf-8")
    assert adapter.halt_reset_at_ms("/repo", "vague") == 0
    ok = _rec(
        type="assistant",
        message={"role": "assistant", "content": [{"type": "text", "text": "fine"}]},
    )
    (proj / "healthy.jsonl").write_text(user + "\n" + ok + "\n", encoding="utf-8")
    assert adapter.halt_reset_at_ms("/repo", "healthy") == 0
    assert adapter.halt_reset_at_ms("/repo", "missing") == 0


def test_claude_version(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    # The most recent versioned record wins (Claude Code can update mid-session).
    (proj / "sid.jsonl").write_text(
        _rec(type="user", version="2.1.181")
        + "\n"
        + _rec(type="assistant", version="2.1.193")
        + "\n",
        encoding="utf-8",
    )
    assert adapter.claude_version("/repo", "sid") == "2.1.193"

    # No version field anywhere, and a missing transcript, both yield None.
    (proj / "noversion.jsonl").write_text(_rec(type="user") + "\n", encoding="utf-8")
    assert adapter.claude_version("/repo", "noversion") is None
    assert adapter.claude_version("/repo", "missing") is None


def test_uses_codex_workflow_scans_transcript_and_mtime_caches(tmp_path: Path) -> None:
    from command_center.adapters import claude as claude_mod

    claude_mod._CODEX_WORKFLOW_CACHE.clear()
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    path = proj / "sid.jsonl"
    base_mtime = 1_782_302_578

    path.write_text(_rec(type="user", message={"role": "user", "content": "normal ask"}) + "\n")
    os.utime(path, (base_mtime, base_mtime))
    assert adapter.uses_codex_workflow("/repo", "sid") is False

    # Same mtime: cached false is reused, like the prompt cache for frozen transcripts.
    path.write_text(
        _rec(
            type="user",
            message={
                "role": "user",
                "content": f"<command-name>/{CODEX_WORKFLOW_NAME}</command-name>",
            },
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(path, (base_mtime, base_mtime))
    assert adapter.uses_codex_workflow("/repo", "sid") is False

    # Mtime moved: a growing transcript is re-read and the command marker is detected.
    os.utime(path, (base_mtime + 1, base_mtime + 1))
    assert adapter.uses_codex_workflow("/repo", "sid") is True
    assert adapter.uses_codex_workflow("/repo", "missing") is False


def test_uses_codex_workflow_ignores_skill_listing_and_doc_mentions(tmp_path: Path) -> None:
    """The bare workflow NAME is injected into EVERY session's ``skill_listing``
    attachment (the available-skills list) and appears in this repo's AGENTS.md prose
    as ``/codex-implement-task-and-claude-review``. Neither is an invocation, so a
    bare-substring scan would mis-badge every session. Only the ``<command-name>/…``
    tag counts."""
    from command_center.adapters import claude as claude_mod

    claude_mod._CODEX_WORKFLOW_CACHE.clear()
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    path = proj / "sid.jsonl"
    path.write_text(
        _rec(
            type="user",
            attachment={
                "type": "skill_listing",
                "content": f"- {CODEX_WORKFLOW_NAME}: Delegate a task to OpenAI Codex.",
            },
        )
        + "\n"
        + _rec(
            type="user",
            message={"role": "user", "content": f"see `/{CODEX_WORKFLOW_NAME}` in the docs"},
        )
        + "\n",
        encoding="utf-8",
    )
    assert adapter.uses_codex_workflow("/repo", "sid") is False


def test_session_effort_parses_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center.adapters import claude as claude_mod

    adapter = ClaudeAdapter()
    commands = {
        4321: "claude --session-id abc --model claude-fable-5 --effort xhigh 'do it'",
        4322: "claude --session-id def --effort=high 'go'",
        4323: "claude --session-id ghi 'no effort flag'",
        4324: "claude --session-id jkl --effort bogus 'invalid level'",
    }
    # Stub the cached ps scan (ppid -> [(pid, command)]) — one child per fake parent.
    monkeypatch.setattr(
        claude_mod, "_children_map", lambda: {1: [(pid, cmd) for pid, cmd in commands.items()]}
    )
    assert adapter.session_effort(4321) == "xhigh"  # --effort <level>
    assert adapter.session_effort(4322) == "high"  # --effort=<level>
    assert adapter.session_effort(4323) is None  # no flag
    assert adapter.session_effort(4324) is None  # invalid level rejected
    assert adapter.session_effort(9999) is None  # pid not in the scan
    assert adapter.session_effort(0) is None  # non-positive pid


def test_observed_model_last_real_and_skips_synthetic(tmp_path: Path) -> None:
    from command_center.adapters import claude as claude_mod

    claude_mod._OBSERVED_MODEL_CACHE.clear()
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)

    def _assistant(model: object) -> str:
        return _rec(
            type="assistant",
            message={
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "x"}],
            },
        )

    # (a) the LAST real model wins over an earlier one.
    (proj / "a.jsonl").write_text(
        _assistant("claude-opus-4-8") + "\n" + _assistant("claude-fable-5") + "\n",
        encoding="utf-8",
    )
    assert adapter.observed_model("/repo", "a") == "claude-fable-5"

    # (b) trailing <synthetic> and missing-model entries are skipped → last REAL wins.
    (proj / "b.jsonl").write_text(
        _assistant("claude-fable-5")
        + "\n"
        + _assistant("<synthetic>")
        + "\n"
        + _rec(type="assistant", message={"role": "assistant", "content": []})
        + "\n",  # no model
        encoding="utf-8",
    )
    assert adapter.observed_model("/repo", "b") == "claude-fable-5"

    # (c) no transcript → None.
    assert adapter.observed_model("/repo", "missing") is None

    # (d) a transcript with no model entries at all → None.
    (proj / "d.jsonl").write_text(
        _rec(type="user", message={"role": "user", "content": "hi"}) + "\n", encoding="utf-8"
    )
    assert adapter.observed_model("/repo", "d") is None


def _assistant_rec(model: object) -> str:
    """An assistant record carrying *model* (the shape ``last_model_in_file`` looks for)."""
    return _rec(
        type="assistant",
        message={"role": "assistant", "model": model, "content": [{"type": "text", "text": "x"}]},
    )


def test_last_model_in_file_reads_the_tail_backwards(tmp_path: Path) -> None:
    """The reverse chunked reader answers with the LAST real model, whatever the file
    ends with — that tail read is what replaced the full forward parse of every stored
    transcript (~27 s of a cold `ccc ls`)."""
    from command_center.adapters.claude import last_model_in_file

    # (a) the model sits in the last line.
    a = tmp_path / "a.jsonl"
    a.write_text(
        _assistant_rec("claude-opus-4-8") + "\n" + _assistant_rec("claude-fable-5") + "\n",
        encoding="utf-8",
    )
    assert last_model_in_file(a) == "claude-fable-5"

    # (b) the last assistant record is the harness's <synthetic> placeholder → skip to the
    # last REAL model behind it.
    b = tmp_path / "b.jsonl"
    b.write_text(
        _assistant_rec("claude-fable-5") + "\n" + _assistant_rec("<synthetic>") + "\n",
        encoding="utf-8",
    )
    assert last_model_in_file(b) == "claude-fable-5"

    # (c) many chunks, and the target record straddles the boundaries (chunk_size=64 is far
    # smaller than one record) — the carry-over must not split a line.
    c = tmp_path / "c.jsonl"
    filler = _rec(type="user", message={"role": "user", "content": "x" * 200}) + "\n"
    c.write_text(
        _assistant_rec("claude-opus-4-8")
        + "\n"
        + _assistant_rec("claude-fable-5")
        + "\n"
        + filler * 5,
        encoding="utf-8",
    )
    assert last_model_in_file(c, chunk_size=64) == "claude-fable-5"

    # (d) a live transcript caught mid-write ends in a partial line: the truncation keeps
    # the model (so the prefilter passes and json really is attempted), the parse fails, and
    # the last COMPLETE record wins.
    d = tmp_path / "d.jsonl"
    partial = _assistant_rec("claude-opus-4-8")[:100]
    assert '"model"' in partial and not partial.endswith("}")
    d.write_text(_assistant_rec("claude-fable-5") + "\n" + partial, encoding="utf-8")
    assert last_model_in_file(d) == "claude-fable-5"

    # (e) no assistant record at all, (f) empty file, (g) blank lines tolerated.
    e = tmp_path / "e.jsonl"
    e.write_text(_rec(type="user", message={"role": "user", "content": "hi"}) + "\n", "utf-8")
    assert last_model_in_file(e) is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert last_model_in_file(empty) is None
    g = tmp_path / "g.jsonl"
    g.write_text("\n\n" + _assistant_rec("claude-fable-5") + "\n\n\n", encoding="utf-8")
    assert last_model_in_file(g) == "claude-fable-5"


def test_codex_marker_in_file_scans_a_byte_range(tmp_path: Path) -> None:
    """The marker scan reports how far it got so the next pass can resume there (the
    transcripts are append-only), and overlaps its chunks so a straddling marker is seen."""
    from command_center.adapters.claude import codex_marker_in_file

    marker_line = (
        _rec(
            type="user",
            message={
                "role": "user",
                "content": f"<command-name>/{CODEX_WORKFLOW_NAME}</command-name>",
            },
        )
        + "\n"
    )
    plain = _rec(type="user", message={"role": "user", "content": "normal ask"}) + "\n"

    absent = tmp_path / "absent.jsonl"
    absent.write_text(plain, encoding="utf-8")
    assert codex_marker_in_file(absent) == (False, absent.stat().st_size)

    present = tmp_path / "present.jsonl"
    present.write_text(plain + marker_line, encoding="utf-8")
    size = present.stat().st_size
    assert codex_marker_in_file(present) == (True, size)

    # A chunk size far smaller than the marker: only the overlap makes it findable.
    assert codex_marker_in_file(present, chunk_size=8) == (True, size)

    # Resuming AFTER the marker never re-finds it; resuming before it does.
    after = len(plain.encode()) + len(marker_line.encode())
    assert codex_marker_in_file(present, start=after) == (False, size)
    assert codex_marker_in_file(present, start=len(plain.encode())) == (True, size)

    # A start past the end means the file was truncated/rewritten → rescan from 0.
    assert codex_marker_in_file(present, start=size + 10_000) == (True, size)


def test_transcript_readers_raise_on_a_read_failure(tmp_path: Path) -> None:
    """A read failure is NOT a fact about the transcript, so both readers raise.

    Swallowing an OSError as ``None`` / ``(False, start)`` would let a transient
    EIO/EACCES be PERSISTED as "no model" / "no marker" in the scan row (see
    ``scan_transcript`` § Property P). Every caller contains it and retries next pass.
    """
    from command_center.adapters.claude import codex_marker_in_file, last_model_in_file

    missing = tmp_path / "nope.jsonl"
    with pytest.raises(OSError):
        last_model_in_file(missing)
    with pytest.raises(OSError):
        codex_marker_in_file(missing)


def test_core_wrappers_contain_the_reader_oserror(tmp_path: Path) -> None:
    """``observed_model`` / ``uses_codex_workflow`` may now raise; the core wrappers that
    feed a row must still degrade to "" / False rather than kill the build."""
    from command_center import core
    from command_center.models import Session

    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    (proj / "sid.jsonl").mkdir()  # resolves (exists) but every open() raises IsADirectoryError

    session = Session(session_id="sid", cwd="/repo")
    with pytest.raises(OSError):
        adapter.observed_model("/repo", "sid")
    with pytest.raises(OSError):
        adapter.uses_codex_workflow("/repo", "sid")
    assert core._observed_model(adapter, session) == ""  # pylint: disable=protected-access
    assert core._uses_codex_workflow(adapter, session) is False  # pylint: disable=protected-access
    # scan_transcript propagates it too, so nothing half-read is ever persisted.
    with pytest.raises(OSError):
        adapter.scan_transcript("/repo", "sid", None)


def test_first_record_is_queue_op_is_tri_state(tmp_path: Path) -> None:
    """``None`` means "not decided yet" and must never collapse to ``False``: the answer
    is persisted (``TranscriptScan.headless``), and a ``False`` frozen in from an empty or
    mid-write transcript would make a headless one-shot look interactive forever."""
    from command_center.adapters.claude import first_record_is_queue_op

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert first_record_is_queue_op(empty) is None

    blanks = tmp_path / "blanks.jsonl"
    blanks.write_text("\n\n", encoding="utf-8")
    assert first_record_is_queue_op(blanks) is None

    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text('{"type": "queue-oper', encoding="utf-8")  # caught mid-write
    assert first_record_is_queue_op(malformed) is None

    assert first_record_is_queue_op(tmp_path / "missing.jsonl") is None

    headless = tmp_path / "headless.jsonl"
    headless.write_text("\n" + _rec(type="queue-operation") + "\n", encoding="utf-8")
    assert first_record_is_queue_op(headless) is True  # leading blank line skipped

    interactive = tmp_path / "interactive.jsonl"
    interactive.write_text(_rec(type="last-prompt") + "\n", encoding="utf-8")
    assert first_record_is_queue_op(interactive) is False


def _adapter_with(tmp_path: Path, name: str, text: str) -> tuple[ClaudeAdapter, Path]:
    """A ClaudeAdapter over *tmp_path* plus a written ``/repo`` transcript for *name*."""
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{name}.jsonl"
    path.write_text(text, encoding="utf-8")
    return ClaudeAdapter(claude_home=tmp_path), path


def test_scan_transcript_headless_is_probed_once_and_carried_on_a_grow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headless fact is persisted, not re-probed: an unchanged file with the fact
    already known costs one stat, and a same-path GROW carries it forward (an append-only
    file's first record cannot change)."""
    from command_center.adapters import claude as claude_mod

    adapter, path = _adapter_with(tmp_path, "sid", _rec(type="queue-operation") + "\n")

    first = adapter.scan_transcript("/repo", "sid", None)
    assert first is not None and first.headless is True

    # Known fact + untouched file → the SAME object (nothing to re-persist).
    assert adapter.scan_transcript("/repo", "sid", first) is first

    # A prior row from before the column existed (headless None) on an UNCHANGED file:
    # the probe runs once and a NEW object comes back so the caller persists it.
    legacy = dataclasses.replace(first, headless=None)
    filled = adapter.scan_transcript("/repo", "sid", legacy)
    assert filled is not None and filled is not legacy and filled.headless is True
    assert (filled.path, filled.mtime_ns, filled.size) == (
        legacy.path,
        legacy.mtime_ns,
        legacy.size,
    )

    # A GROW on the same path carries the fact forward without opening the first record.
    def _boom(*_args: object, **_kwargs: object) -> bool | None:
        raise AssertionError("a same-path grow must not re-probe the first record")

    monkeypatch.setattr(claude_mod, "first_record_is_queue_op", _boom)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_assistant_rec("claude-fable-5") + "\n")
    grown = adapter.scan_transcript("/repo", "sid", first)
    assert grown is not None and grown is not first and grown.headless is True


def _padded(record: str, size: int) -> str:
    """*record* as the first line of a transcript, space-padded to exactly *size* bytes.

    Lets a test rewrite a transcript at a CHOSEN byte size (the identity `scan_transcript`
    pins) with a different first record.
    """
    line = record + "\n"
    assert len(line) <= size
    return line + " " * (size - len(line))


def test_scan_transcript_headless_is_reprobed_on_any_other_identity_change(
    tmp_path: Path,
) -> None:
    """A same-size rewrite and a truncation are not appends, so the first record CAN have
    changed — both re-probe rather than carry the stale fact."""
    adapter, path = _adapter_with(tmp_path, "sid", _padded(_rec(type="queue-operation"), 200))
    first = adapter.scan_transcript("/repo", "sid", None)
    assert first is not None and first.headless is True and first.size == 200

    # (a) same-size rewrite (different mtime): re-probed, so the fact FLIPS.
    path.write_text(_padded(_rec(type="last-prompt"), 200), encoding="utf-8")
    bump = first.mtime_ns + 1_000_000_000
    os.utime(path, ns=(bump, bump))
    rewritten = adapter.scan_transcript("/repo", "sid", first)
    assert rewritten is not None and rewritten.size == first.size  # same identity size
    assert rewritten.headless is False

    # (b) truncation / replacement: strictly smaller → re-probed, flips back.
    path.write_text(_padded(_rec(type="queue-operation"), 100), encoding="utf-8")
    smaller = adapter.scan_transcript("/repo", "sid", rewritten)
    assert smaller is not None and smaller.size < rewritten.size
    assert smaller.headless is True


def test_scan_transcript_undetermined_headless_writes_nothing(tmp_path: Path) -> None:
    """An empty / mid-write transcript learns nothing, so the prior row comes back
    UNCHANGED (identity) — otherwise every pass would rewrite that row forever."""
    adapter, path = _adapter_with(tmp_path, "sid", "")

    first = adapter.scan_transcript("/repo", "sid", None)
    assert first is not None and first.headless is None  # empty file → undetermined

    # Same object back: nothing was learned this pass, so nothing is persisted.
    assert adapter.scan_transcript("/repo", "sid", first) is first

    # Appended for real → the fact finally lands.
    path.write_text(_rec(type="queue-operation") + "\n", encoding="utf-8")
    second = adapter.scan_transcript("/repo", "sid", first)
    assert second is not None and second.headless is True

    # A malformed first record is the same kind of "not yet": None, then the real value
    # once the line is complete.
    _, broken = _adapter_with(tmp_path, "broken", '{"type": "queue-oper')
    partial = adapter.scan_transcript("/repo", "broken", None)
    assert partial is not None and partial.headless is None
    broken.write_text(_rec(type="queue-operation") + "\n", encoding="utf-8")
    repaired = adapter.scan_transcript("/repo", "broken", partial)
    assert repaired is not None and repaired.headless is True


def test_scan_transcript_clamps_the_scanned_offset_to_the_pinned_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Property P: ``codex_scanned_to <= size`` in every persisted row.

    ``codex_marker_in_file`` reports the size IT saw at its own open, which on a live
    transcript can exceed the ``stat()`` taken a moment earlier. An unclamped offset
    would then let a later SAME-SIZE rewrite resume past bytes that were never scanned
    and miss the marker in them.
    """
    from command_center.adapters import claude as claude_mod

    body = "x" * 99 + "\n"
    adapter, path = _adapter_with(tmp_path, "sid", body)
    assert path.stat().st_size == 100

    monkeypatch.setattr(claude_mod, "codex_marker_in_file", lambda *_a, **_k: (False, 200))
    row = adapter.scan_transcript("/repo", "sid", None)
    assert row is not None and row.size == 100
    assert row.codex_scanned_to == 100  # clamped to the size THIS row pins

    # Now the file really is 200 bytes with the marker at byte 120 — past the clamped
    # offset, so the real scan resumes from 100 and still finds it. Had the row kept the
    # unclamped 200 it would have resumed at 200 and missed the marker entirely.
    monkeypatch.undo()
    marker = claude_mod._CODEX_WORKFLOW_MARKER  # pylint: disable=protected-access
    tail = 199 - 120 - len(marker)
    assert tail >= 0
    path.write_text("y" * 120 + marker + "z" * tail + "\n", encoding="utf-8")
    assert path.stat().st_size == 200
    os.utime(path, ns=(row.mtime_ns + 1_000_000_000, row.mtime_ns + 1_000_000_000))
    second = adapter.scan_transcript("/repo", "sid", row)
    assert second is not None and second.codex is True


def test_scan_transcript_survives_a_stale_concurrent_writer(tmp_path: Path) -> None:
    """Property P: whichever of two scanners persisted last, the next pass re-derives
    every fact from the CURRENT file — a stale row costs one bounded re-read, never a
    wrong answer (the store deliberately carries no ordering guard)."""
    adapter, path = _adapter_with(tmp_path, "sid", _assistant_rec("claude-opus-4-8") + "\n")
    scan_a = adapter.scan_transcript("/repo", "sid", None)  # v1: no marker
    assert scan_a is not None and scan_a.codex is False

    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            _rec(
                type="user",
                message={
                    "role": "user",
                    "content": f"<command-name>/{CODEX_WORKFLOW_NAME}</command-name>",
                },
            )
            + "\n"
        )
        handle.write(_assistant_rec("claude-fable-5") + "\n")
    scan_b = adapter.scan_transcript("/repo", "sid", scan_a)
    assert scan_b is not None and scan_b.codex is True

    # A peer re-persisted the STALE v1 row; this pass starts from it again and still lands
    # on the truth (marker found, model from the tail).
    again = adapter.scan_transcript("/repo", "sid", scan_a)
    assert again is not None and again.codex is True
    assert again.model == "claude-fable-5"
    assert again.codex_scanned_to <= again.size  # the invariant holds on every row


def test_scan_transcript_reuses_the_prior_row_when_nothing_changed(tmp_path: Path) -> None:
    """An unchanged transcript costs one stat() and hands back the SAME object — the
    identity callers use to know there is nothing to re-persist."""
    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    path = proj / "sid.jsonl"
    path.write_text(_assistant_rec("claude-opus-4-8") + "\n", encoding="utf-8")

    first = adapter.scan_transcript("/repo", "sid", None)
    assert first is not None
    assert first.session_id == "sid" and first.path == str(path)
    assert first.model == "claude-opus-4-8" and first.codex is False
    assert first.size == path.stat().st_size and first.codex_scanned_to == first.size
    assert first.scanned_at > 0

    assert adapter.scan_transcript("/repo", "sid", first) is first  # identity, not equality

    # An appended turn on a NEW model → a fresh row (bigger, re-read).
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_assistant_rec("claude-fable-5") + "\n")
    second = adapter.scan_transcript("/repo", "sid", first)
    assert second is not None and second is not first
    assert second.model == "claude-fable-5" and second.size > first.size

    assert adapter.scan_transcript("/repo", "missing", None) is None  # no transcript


def test_scan_transcript_codex_marker_is_incremental_and_sticky(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker scan only reads the bytes appended since the last pass, and a marker
    already seen is never looked for again (it cannot un-happen)."""
    from command_center.adapters import claude as claude_mod

    adapter = ClaudeAdapter(claude_home=tmp_path)
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    path = proj / "sid.jsonl"
    path.write_text(_rec(type="user", message={"role": "user", "content": "hi"}) + "\n", "utf-8")

    first = adapter.scan_transcript("/repo", "sid", None)
    assert first is not None and first.codex is False
    assert first.codex_scanned_to == first.size  # covered the whole file

    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            _rec(
                type="user",
                message={
                    "role": "user",
                    "content": f"<command-name>/{CODEX_WORKFLOW_NAME}</command-name>",
                },
            )
            + "\n"
        )
    second = adapter.scan_transcript("/repo", "sid", first)
    assert second is not None and second.codex is True

    # Sticky: with the marker already recorded the scan is not run again at all.
    def _boom(*_args: object, **_kwargs: object) -> tuple[bool, int]:
        raise AssertionError("codex_marker_in_file must not run once the marker is known")

    monkeypatch.setattr(claude_mod, "codex_marker_in_file", _boom)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_assistant_rec("claude-fable-5") + "\n")
    third = adapter.scan_transcript("/repo", "sid", second)
    assert third is not None and third.codex is True
    assert third.codex_scanned_to == third.size


def test_todos(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    assert adapter.todos("sid") == []
    task_dir = tmp_path / "tasks" / "sid"
    task_dir.mkdir(parents=True)
    (task_dir / "1.json").write_text(
        json.dumps({"subject": "first", "status": "completed"}), encoding="utf-8"
    )
    (task_dir / "2.json").write_text(
        json.dumps({"subject": "second", "status": "in_progress"}), encoding="utf-8"
    )
    assert adapter.todos("sid") == [("completed", "first"), ("in_progress", "second")]


def test_has_subagent_ignores_dot_claude_path_helpers(tmp_path: Path, monkeypatch) -> None:
    """An idle session whose only descendants are ``~/.claude/``-pathed helpers
    (the per-refresh statusline command, hook scripts) must NOT read as a subagent —
    the substring ``claude`` in their *path* used to flip an idle row to the green ▶.
    """
    from command_center.adapters import claude as claude_mod

    adapter = ClaudeAdapter(claude_home=tmp_path)
    # pid 100 (the session) -> 101 statusline helper -> (nothing claude-the-program)
    fake_tree = {
        100: [(101, "bash /home/user/.claude/statusline-command.sh")],
        101: [(102, "node /home/user/.claude/hooks/some-hook.js")],
    }
    monkeypatch.setattr(claude_mod, "_children_map", lambda: fake_tree)
    assert adapter.has_subagent(100) is False


def test_has_subagent_detects_real_claude_p_subagent(tmp_path: Path, monkeypatch) -> None:
    """A genuine ``claude -p …`` child (bare or absolute-path argv[0]) still counts."""
    from command_center.adapters import claude as claude_mod

    adapter = ClaudeAdapter(claude_home=tmp_path)
    bare = {100: [(101, "claude -p Summarize the transcript --model claude-haiku-4-5")]}
    monkeypatch.setattr(claude_mod, "_children_map", lambda: bare)
    assert adapter.has_subagent(100) is True

    absolute = {100: [(101, "/home/user/.bun/bin/claude -p do-thing")]}
    monkeypatch.setattr(claude_mod, "_children_map", lambda: absolute)
    assert adapter.has_subagent(100) is True

    # ccc's own CLI is not a subagent even though it lives under command-center.
    ccc = {100: [(101, "/home/user/.local/bin/ccc daemon")]}
    monkeypatch.setattr(claude_mod, "_children_map", lambda: ccc)
    assert adapter.has_subagent(100) is False


def test_has_background_task(tmp_path: Path, monkeypatch) -> None:
    """A live Bash-tool shell descendant (``shell-snapshots/snapshot-…``) is a
    background task; persistent helpers (MCP node, caffeinate) are not."""
    from command_center.adapters import claude as claude_mod

    adapter = ClaudeAdapter(claude_home=tmp_path)
    snap = "/home/user/.claude/shell-snapshots/snapshot-zsh-123.sh"
    # session 100 -> snapshot zsh -> the actual backgrounded command
    bg_tree = {
        100: [(101, f"/bin/zsh -c source {snap} 2>/dev/null || true && exec sleep 90")],
        101: [(102, "sleep 90")],
    }
    monkeypatch.setattr(claude_mod, "_children_map", lambda: bg_tree)
    assert adapter.has_background_task(100) is True

    # Only persistent helpers (MCP server, caffeinate) → NOT a background task.
    helpers = {
        100: [
            (201, "caffeinate -i -t 300"),
            (202, "npm exec @playwright/mcp@0.0.76 --cdp-endpoint http://localhost:9222"),
        ],
        202: [(203, "node /home/user/.npm/_npx/abc/playwright-mcp")],
    }
    monkeypatch.setattr(claude_mod, "_children_map", lambda: helpers)
    assert adapter.has_background_task(100) is False
    assert adapter.has_background_task(0) is False  # guard


def test_probe(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    assert adapter.probe() is False
    _write_session(tmp_path, os.getpid(), "s", "/c")
    assert adapter.probe() is True


# ---------------------------------------------------------------------------
# session_events (the normalized stream behind the full-session rendering)
# ---------------------------------------------------------------------------
def _events_transcript(home: Path, records: list[dict]) -> Path:
    proj = home / "projects" / "-Users-x-repo"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / "sid.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_events_in_file_pairs_tools_and_filters(tmp_path: Path) -> None:
    from command_center.adapters.claude import events_in_file

    records: list[dict] = [
        {"type": "user", "message": {"role": "user", "content": "fix the failing test"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "secret reasoning"},
                    {"type": "text", "text": "Looking at the test."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "pytest -x"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "1 failed"}],
                    },
                ],
            },
        },
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "subagent chatter"}],
            },
        },
        {"type": "assistant", "message": {"role": "assistant", "content": "Fixed."}},
    ]
    events = events_in_file(_events_transcript(tmp_path, records))
    assert [e.kind for e in events] == ["prompt", "text", "tool", "text"]
    tool = events[2]
    assert tool.tool_name == "Bash"
    assert tool.tool_input == {"command": "pytest -x"}
    assert tool.tool_result == "1 failed"  # paired by tool_use_id
    joined = " ".join(e.text for e in events)
    assert "secret reasoning" not in joined  # thinking skipped
    assert "subagent chatter" not in joined  # sidechain skipped


def test_events_prompts_align_with_all_user_prompts(tmp_path: Path) -> None:
    """Prompt events use the SAME filter as all_user_prompts → identical (N) indexing."""
    from command_center.adapters.claude import events_in_file

    lone_notification = "<task-notification><task-id>a</task-id></task-notification>"
    records: list[dict] = [
        {"type": "user", "message": {"role": "user", "content": "one"}},
        {"type": "user", "message": {"role": "user", "content": lone_notification}},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
        {"type": "user", "message": {"role": "user", "content": "two"}},
    ]
    path = _events_transcript(tmp_path, records)
    adapter = ClaudeAdapter(claude_home=tmp_path)
    prompts = [e.text for e in events_in_file(path) if e.kind == "prompt"]
    assert prompts == adapter.all_user_prompts_in_file(path) == ["one", "two"]


def test_session_events_missing_transcript(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(claude_home=tmp_path)
    assert adapter.session_events("/Users/x/none", "missing") == []
