"""Contract test for the fresh-install INERT defaults.

A bare ``ccc`` install must spend ZERO LLM tokens, spawn NO external tools
(``gh`` / ``codex`` / ``claude -p`` / the resume watcher), auto-close NOTHING, and
write ONLY under ``CLAUDE_HOME`` — until the user opts in (a future ``ccc init``
wizard). This module pins that contract:

* every :data:`config.INERT_DEFAULT_KEYS` member is ``False`` in ``DEFAULTS`` (and the
  set is exactly the expected one),
* with the default config and a temp ``CLAUDE_HOME``, the hook entry points and one
  daemon pass spawn NOTHING and write nowhere outside ``CLAUDE_HOME``,
* ``short_aim_backend = "auto"`` resolves to ``claude`` when the codex CLI is absent.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from command_center import config, daemon, hooks, short_aim
from command_center import spawn as spawn_mod
from command_center.models import now_ms
from command_center.store import Store

# The exact inert set, spelled out independently of the module so a drift in either
# direction (a key added/removed, or a value flipped back to True) fails loudly.
_EXPECTED_INERT_KEYS = {
    "future_files",
    "mirror_running",
    "mirror_done",
    "mirror_sessions",
    "copilot_usage",
    "claude_usage",
    "codex_usage",
    "resume_halted",
    "reap",
    "short_aim",
    "aim_score_on_set",
    "grade_on_turn",
    "assess_aim_on_turn",
    "drift_check",
    "summarize",
    "autoprogress",
    "verify_subgoals_llm",
}


def test_inert_default_keys_match_and_are_all_false() -> None:
    """INERT_DEFAULT_KEYS is exactly the expected set and every member defaults False."""
    assert set(config.INERT_DEFAULT_KEYS) == _EXPECTED_INERT_KEYS
    assert len(config.INERT_DEFAULT_KEYS) == len(_EXPECTED_INERT_KEYS)  # no dupes
    default_cfg = config.Config()
    for key in config.INERT_DEFAULT_KEYS:
        assert config.DEFAULTS[key] is False, f"{key} must be False in DEFAULTS (inert contract)"
        assert getattr(default_cfg, key) is False, f"Config().{key} must default False"


def test_short_aim_backend_auto_resolves_to_claude_without_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "auto" prefers codex when on PATH, falls back to claude otherwise; explicit values stay."""
    # Codex absent from PATH -> auto falls back to the claude backend.
    monkeypatch.setattr(short_aim.shutil, "which", lambda _name: None)
    assert short_aim.resolve_backend("auto") == "claude"

    # Codex present -> auto prefers it (keeps the cost off Claude tokens).
    monkeypatch.setattr(
        short_aim.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None
    )
    assert short_aim.resolve_backend("auto") == "codex"

    # Explicit values always pass through unchanged (no PATH probe).
    monkeypatch.setattr(short_aim.shutil, "which", lambda _name: None)
    assert short_aim.resolve_backend("codex") == "codex"
    assert short_aim.resolve_backend("claude") == "claude"

    # The shipped default is "auto".
    assert config.Config().short_aim_backend == "auto"


def _files_outside(root: Path, exclude: Path) -> dict[str, bytes]:
    """Map ``relpath -> content`` for every file under *root* NOT inside *exclude*."""
    out: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file() and exclude not in path.parents:
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


def test_inert_defaults_spawn_nothing_and_write_only_under_claude_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # pylint: disable=too-many-locals
    """Hook entry points + a daemon pass: no spawn, no write outside CLAUDE_HOME."""
    # State (SQLite, events.log, caches) lives under CLAUDE_HOME — the ONLY tree the inert
    # contract permits writing to.
    claude_home = tmp_path / "claude_home"
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    # A temp HOME too, so any stray "~/…" write would surface in the outside-snapshot.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    # Record every ccc re-spawn (the token spenders relaunch ccc detached)...
    spawned: list[list[str]] = []

    def _rec_spawn(args: list[str]) -> bool:
        spawned.append(list(args))
        return True

    monkeypatch.setattr(spawn_mod, "spawn_ccc", _rec_spawn)

    # ...and every external-tool launch (gh / codex / claude -p / pgrep / osascript).
    subproc_calls: list[tuple[str, tuple, dict]] = []

    def _rec_run(*a: object, **k: object) -> types.SimpleNamespace:
        subproc_calls.append(("run", a, k))
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    def _rec_popen(*a: object, **k: object) -> types.SimpleNamespace:
        subproc_calls.append(("Popen", a, k))
        return types.SimpleNamespace(
            pid=-1, poll=lambda: 0, wait=lambda *_x: 0, communicate=lambda *_x: ("", "")
        )

    monkeypatch.setattr(subprocess, "run", _rec_run)
    monkeypatch.setattr(subprocess, "Popen", _rec_popen)

    # A deadline/stale alert is allowed on a fresh install, but the seeded session triggers
    # none; still, keep the macOS notifier from shelling out so the test stays hermetic.
    monkeypatch.setattr(daemon, "notify", lambda *a, **k: None)

    # Seed a session REACHABLE by every spend path (has an AIM, an unscored sentinel, a
    # checklist, needs_summary) so a regression that ignored a kill-switch would spawn here.
    # With the inert defaults nothing may.
    with Store() as store:
        store.ensure("s1", cwd=str(tmp_path / "repo"))
        store.set_aim("s1", "ship the usage panel: pytest -q green and the card renders")
        store.set_subgoals("s1", ["render the card", "add a passing test"], source="agent")
        store.update_fields(
            "s1",
            aim_score=-1,  # unscored -> backfill path reachable (must NOT spawn score-aim)
            version="2.1.193",  # version set -> no version-backfill transcript read
            needs_summary=True,  # summary path reachable (must NOT run when summarize off)
            last_response_at=now_ms(),  # recent -> no stale alert
        )

    before = _files_outside(tmp_path, claude_home)

    # 1) Hook entry points (user-prompt / post-tool-use / stop).
    payload = {"session_id": "s1", "cwd": str(tmp_path / "repo")}
    hooks.handle_user_prompt(dict(payload))
    hooks.handle_post_tool_use({**payload, "tool_name": "TodoWrite", "tool_input": {"todos": []}})
    hooks.handle_stop(dict(payload))

    # 2) One full daemon pass (not a dry run).
    daemon.run_once(dry_run=False)

    assert not spawned, f"inert defaults re-spawned ccc: {spawned}"
    assert not subproc_calls, f"inert defaults launched an external subprocess: {subproc_calls}"

    after = _files_outside(tmp_path, claude_home)
    assert after == before, (
        "inert defaults wrote outside CLAUDE_HOME: "
        f"{sorted(set(after) - set(before)) or 'existing files changed'}"
    )
